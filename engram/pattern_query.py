"""The Engram pattern pipeline.

One turn's sentences are interpreted, matched against the speaker's and the shared
pattern categories, and answered by rendering the matched statement's template, with
graph recall and the configured fallback when nothing matches. Each function that takes
``engram`` is bound on ``engram.core.Engram`` as the method of the same name.
"""

from copy import deepcopy
from logging import getLogger as logging_getLogger

from engram import sessions as sessions_mod
from engram.constants_artifacts import Tier
from engram.constants_dialogue import DIALOGUE_ACKNOWLEDGMENT, DIALOGUE_EMOTION, DIALOGUE_OPINION
from engram.constants_language import KIND_STATEMENT, MAX_FACT_SENTENCE_WORDS, MAX_PATTERN_WORDS
from engram.constants_service import MAX_REQUEST_BYTES
from engram.dialogue import (
    classify_dialogue_act,
    conversational_fact_admission,
    dialogue_act_clears_unreferenced_topic,
    extract_dialogue_entities,
    infer_active_topic,
    select_turn_candidate,
    topic_from_statement_pattern,
    topic_is_referenced,
)
from engram.errors import InvalidRequestError
from engram.facts_spacy import extract_facts
from engram.graph import projection_timestamp
from engram.models import (
    record_statement_hit,
    record_statement_query,
    session_record_input,
    session_update_context,
    session_update_dialogue,
)
from engram.nlp import extract_fact, input_kind
from engram.pattern import match_first, pattern_word_count
from engram.polish import polish_response
from engram.substitutions import expand_contractions, get_all_input_subs, split_sentences
from engram.template import template_context
from engram.text import restore_capture_case
from engram.validation import format_utc_timestamp

logger = logging_getLogger("engram.core")


def match_for(engram, owner: str, text: str, that: str = "", topic: str = "") -> tuple:
    """Match one sentence in ``owner``'s chat; return (owner of the match, match) or ().

    ``owner``'s own facts are tried before the shared categories at each
    matcher stage, and no other person's facts are consulted. The match
    owner is "" when a shared category answered. The caller holds
    statement_lock.
    """
    scoped = [("", engram.pattern_matcher)]
    if owner in engram.person_matchers:
        scoped.insert(0, (owner, engram.person_matchers.get(owner, engram.pattern_matcher)))
    found = match_first(tuple(matcher for _, matcher in scoped), text, that=that, topic=topic)
    if not found:
        result = ()
        return result
    position, matched = found
    result = (scoped[position][0], matched)
    return result


def statement_for_match(engram, matched_pattern: str, matched_topic: str, matched_that: str, owner: str) -> dict:
    """Return ``owner``'s statement carrying a matched (pattern, topic, that), or {}.

    ``owner`` is the owner of the matcher that matched ("" for shared). A
    statement carries a pattern as its own or as an alias. Among
    duplicates the highest priority wins, ties going to the earliest
    stored. The caller holds statement_lock.
    """
    selected: dict = {}
    for statement_id in engram.pattern_statements.get(matched_pattern, ()):
        # pattern_statements lists only stored statements (store and eviction keep them in step).
        stmt = engram.statement_by_id.get(statement_id, {})
        if not stmt or stmt.get("introduced_by_user_id", "") != owner:
            continue
        triple_match = stmt.get("topic", "") == matched_topic and stmt.get("that", "") == matched_that
        if triple_match and (not selected or stmt.get("priority", 0) > selected.get("priority", 0)):
            selected = stmt
    result = selected
    return result


def expand_for_match(engram, text: str) -> str:
    """Expand contractions before the graphmaster sees a sentence, that, or topic.

    Hyphen splitting and the last sentence of ``that`` happen inside the
    matcher. Spell correction stays on the keyword path.
    """
    if not text or not engram.config.get("expand_contractions", False):
        result = text
        return result
    result = expand_contractions(text, get_all_input_subs(engram.substitution_maps))
    return result


def pattern_query(
    engram, text: str, context_id: str = "", user_id: str = "", *, include_graph: bool = True, evaluation_time: str = ""
) -> tuple:
    """Query using AIML-style pattern matching.

    Supports multi-sentence input: sentences are split, matched independently,
    and responses are combined.

    Args:
        text: User input text (may contain multiple sentences).
        context_id: Optional conversation context used for turn state.
        user_id: The speaker; empty means the anonymous user "0". Facts
            the turn teaches belong to the speaker, and only the speaker's
            own facts and shared categories can answer.
        include_graph: Whether a no-pattern result may fall back to graph
            recall. The shared response pipeline disables this and applies
            graph precedence once for the complete turn.
        evaluation_time: Optional canonical UTC timestamp for graph recall.

    Returns:
        Tuple of (matched_statement, captured_wildcards, response_text) or ().
        For multi-sentence input, the response joins every matched sentence
        and the statement is the last one matched. The stored dialogue act
        still comes from the sentence that represents the turn.
    """
    if len(text.encode("utf-8", "surrogatepass")) > MAX_REQUEST_BYTES:
        raise InvalidRequestError(f"text exceeds the UTF-8 limit of {MAX_REQUEST_BYTES} bytes")
    with engram.count_lock:
        engram.query_count += 1

    speaker = sessions_mod.normalize_user_id(user_id)

    processed_text = text
    if engram.config.get("expand_contractions", False):
        processed_text = expand_contractions(text, get_all_input_subs(engram.substitution_maps))

    sentences = split_sentences(processed_text)
    if not sentences:
        # No sentences found, treat as single input
        sentences = [processed_text] if processed_text.strip() else []

    if not sentences:
        result = ()
        return result

    # One clock read per turn stamps hits and session activity and supplies the default evaluation time.
    turn_moment = engram.clock()
    selected_evaluation_time = evaluation_time or format_utc_timestamp(turn_moment)
    projection_timestamp(selected_evaluation_time, True, "pattern query evaluation time")

    session = {}
    that = ""
    topic = ""
    active_topic = ""
    if context_id:
        with engram.session_lock:
            session = sessions_mod.get_session(engram, context_id, create_if_missing=True)
            if session:
                that = session.get("previous_response", "")
                topic = session.get("predicates", {}).get("topic", "")
                active_topic = session.get("active_topic", "")

    responses: list[str] = []
    candidates: list[dict] = []
    turn_dialogue_acts: list[str] = []
    turn_entities: list[dict] = []
    turn_fact_admissions: list[dict] = []

    for sentence in sentences:
        # Interpret every sentence before response selection.  Fact
        # admission is intentionally narrower than extraction: transient,
        # hedged, and conversation-meta assertions can shape the current
        # turn without becoming shared durable knowledge.
        extracted_facts = []
        if engram.config.get("learn_user_facts", False) and len(sentence.split()) <= MAX_FACT_SENTENCE_WORDS:
            if engram.config.get("use_spacy_facts", False):
                extracted_facts = extract_facts(sentence)
            else:
                extracted = extract_fact(sentence)
                extracted_facts = [extracted] if extracted else []
        if extracted_facts and input_kind(sentence) != KIND_STATEMENT:
            extracted_facts = []
        fact_decisions = []
        for fact in extracted_facts:
            decision = conversational_fact_admission(fact, sentence)
            diagnostic = {
                "text": fact.get("original", sentence),
                "subject": fact.get("subject", ""),
                "admitted": decision.get("admitted", False),
                "reason": decision.get("reason", ""),
            }
            fact_decisions.append((fact, decision))
            turn_fact_admissions.append(diagnostic)
        admitted_facts = [fact for fact, decision in fact_decisions if decision.get("admitted", False)]

        prior_topic = active_topic
        sentence_act = classify_dialogue_act(sentence, extracted_facts[0] if extracted_facts else {})
        preliminary_topic = infer_active_topic(
            sentence, fact=extracted_facts[0] if extracted_facts else {}, previous_topic=prior_topic
        )
        sentence_entities = (
            extract_dialogue_entities(sentence, fact=admitted_facts[0] if admitted_facts else {}, topic=preliminary_topic)
            if session
            else []
        )
        inferred_topic = infer_active_topic(
            sentence,
            fact=extracted_facts[0] if extracted_facts else {},
            entities=(sentence_entities if dialogue_act_clears_unreferenced_topic(sentence_act) else []),
            previous_topic=prior_topic,
        )
        explicit_reference = topic_is_referenced(sentence, prior_topic)
        implicit_reply = sentence_act in {DIALOGUE_ACKNOWLEDGMENT, DIALOGUE_EMOTION, DIALOGUE_OPINION} and topic_is_referenced(
            that, prior_topic
        )
        topic_grounded = bool(inferred_topic or explicit_reference or implicit_reply)
        if inferred_topic:
            active_topic = inferred_topic
        elif topic_grounded:
            active_topic = prior_topic
        elif dialogue_act_clears_unreferenced_topic(sentence_act):
            active_topic = ""
        turn_dialogue_acts.append(sentence_act)
        turn_entities.extend(sentence_entities)

        with engram.statement_lock:
            # ``sentence`` was already expanded before it was split. Expanding
            # it again would apply a custom substitution twice.
            scoped_match = engram.match_for(
                speaker, sentence, that=engram.expand_for_match(that), topic=engram.expand_for_match(topic)
            )

        # Fact admission belongs to the observed user turn, not to the
        # availability of a scripted response. Match first so a newly
        # learned fact cannot answer the sentence that introduced it,
        # then persist every admitted fact regardless of match outcome.
        for fact in admitted_facts:
            engram.learn_fact(fact, introduced_by_user_id=speaker)

        if scoped_match:
            match_owner, result = scoped_match
            _, captured, thatstars, topicstars, matched_pattern, matched_topic, matched_that = result
            captured = restore_capture_case(captured, sentence)

            # Find the statement carrying this (pattern, topic, that).
            # Among duplicates the highest priority wins, ties going to
            # the earliest stored.
            with engram.mutation_lock, engram.statement_lock:
                selected = engram.statement_for_match(matched_pattern, matched_topic, matched_that, match_owner)
                if selected:
                    # Pattern selection is a query and a hit in one step
                    # (there is no later confirmation on this path), so
                    # record both -- this is what keeps frequently used
                    # patterns alive under least-recently-used eviction.
                    record_statement_query(selected)
                    record_statement_hit(selected, turn_moment)

                    response_state = {"unknown": False}
                    final_response = engram.process_statement_template(
                        selected,
                        captured,
                        sentence,
                        session,
                        thatstars=thatstars,
                        topicstars=topicstars,
                        that=that,
                        topic=topic,
                        evaluation_time=selected_evaluation_time,
                        speaker=speaker,
                        response_state=response_state,
                    )
                    # Output cleanup: repair casing (sentence starts, the
                    # pronoun I) that lowercase captures splice into
                    # authored text. Text that rendered exactly as authored
                    # is kept, so code such as "s[1:4]" or "pop()" survives.
                    authored = selected.get("template", {}) or selected.get("text", "")
                    if engram.config.get("polish_responses", False) and final_response != authored:
                        final_response = polish_response(final_response)
                    responses.append(final_response)
                    candidates.append(
                        {
                            "statement": selected,
                            "captured": captured,
                            "response": final_response,
                            "dialogue_act": sentence_act,
                            "unknown": response_state.get("unknown", False),
                        }
                    )

                    that = final_response
                    if session:
                        with engram.session_lock:
                            topic = session.get("predicates", {}).get("topic", "")

    if not responses:
        selected_act = turn_dialogue_acts[-1] if turn_dialogue_acts else ""
        graph_response = engram.graph_lookup(text, evaluation_time=selected_evaluation_time) if include_graph else ""
        if graph_response:
            if session:
                with engram.session_lock:
                    session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                    session_update_context(session, graph_response, text, moment=turn_moment)
            graph_result_tuple = ({}, [], graph_response)
            return graph_result_tuple

        fallback_response = engram.config.get("fallback_response", "")
        if fallback_response:
            if session:
                with engram.session_lock:
                    session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                    session_update_context(session, fallback_response, text, moment=turn_moment)
            fallback_tuple = ({}, [], fallback_response)
            return fallback_tuple
        if session:
            with engram.session_lock:
                session_update_dialogue(session, selected_act, active_topic, turn_entities, turn_fact_admissions)
                session_record_input(session, text, turn_moment)
        result = ()
        return result

    selected_candidate = select_turn_candidate(candidates)
    combined_response = " ".join(responses)
    returned_stmt = candidates[-1].get("statement", {})
    returned_captured = candidates[-1].get("captured", [])

    # A successful learned-fact recall is direct evidence of the new
    # topic, even when the query used an inverse alias such as
    # "What is good?" -> Sushi.
    if returned_stmt.get("pattern_aliases", []):
        recalled_topic = topic_from_statement_pattern(returned_stmt.get("pattern", ""), returned_stmt.get("text", ""))
        if recalled_topic:
            active_topic = recalled_topic

    if session:
        with engram.session_lock:
            session_update_dialogue(
                session, selected_candidate.get("dialogue_act", ""), active_topic, turn_entities, turn_fact_admissions
            )
            session_update_context(session, combined_response, text, moment=turn_moment)

    with engram.statement_lock:
        returned_copy = deepcopy(returned_stmt)
    returned_copy["unknown"] = any(candidate.get("unknown", False) for candidate in candidates)
    match_tuple = (returned_copy, returned_captured, combined_response)
    return match_tuple


def process_statement_template(
    engram,
    stmt: dict,
    captured: list[str],
    input_text: str,
    session,
    thatstars=(),
    topicstars=(),
    that: str = "",
    topic: str = "",
    evaluation_time: str = "",
    *,
    speaker: str,
    response_state: dict,
) -> str:
    """Process a statement's template with context.

    Args:
        stmt: The matched statement.
        captured: Wildcard captures from pattern matching.
        input_text: Original user input.
        session: Optional session for context.
        thatstars: Wildcard captures from that pattern.
        topicstars: Wildcard captures from topic pattern.
        speaker: The person whose turn this is. A redirect matches in
            their chat's scope, and a category the template teaches
            (``<learn>``) belongs to them.
        response_state: This rendered candidate's native unknown marker, shared with redirects.

    Returns:
        Processed response string.
    """
    # Build template context (even for plain text to support {bot:name} etc.)
    context = template_context(
        stars=captured,
        thatstars=thatstars or (),
        topicstars=topicstars or (),
        input_text=input_text,
        request_text=input_text,
        bot=engram.pattern_matcher.bot_properties(),
        maps=engram.maps,
        person_subs=engram.substitution_maps.get("person", {}),
        person2_subs=engram.substitution_maps.get("person2", {}),
        gender_subs=engram.substitution_maps.get("gender", {}),
        category_count=len(engram.statements),
        graph_fn=engram.graph_read_fn,
        evaluation_time=evaluation_time,
    )

    context["response_state"] = response_state
    if session:
        with engram.session_lock:
            context["session_id"] = session.get("session_id", "")
            context["predicates"] = session.get("predicates", {}).copy()
            context["input_history"] = session.get("input_history", []).copy()
            context["response_history"] = session.get("response_history", []).copy()
            context["that_history"] = [s.copy() for s in session.get("that_history", [])]

    def redirect_fn(pattern: str) -> str:
        with engram.statement_lock:
            scoped_match = engram.match_for(
                speaker, engram.expand_for_match(pattern), that=engram.expand_for_match(that), topic=engram.expand_for_match(topic)
            )
        if scoped_match:
            match_owner, result = scoped_match
            _, new_captured, new_thatstars, new_topicstars, matched_pattern, matched_topic, matched_that = result
            with engram.mutation_lock, engram.statement_lock:
                redirect_stmt = engram.statement_for_match(matched_pattern, matched_topic, matched_that, match_owner)
                if redirect_stmt:
                    new_context = template_context(
                        stars=new_captured,
                        thatstars=new_thatstars,
                        topicstars=new_topicstars,
                        input_text=pattern,
                        request_text=input_text,
                        bot=context.get("bot", {}),
                        maps=context.get("maps", {}),
                        person_subs=context.get("person_subs", {}),
                        person2_subs=context.get("person2_subs", {}),
                        gender_subs=context.get("gender_subs", {}),
                        predicates=context.get("predicates", {}),
                        input_history=context.get("input_history", []),
                        response_history=context.get("response_history", []),
                        that_history=context.get("that_history", []),
                        session_id=context.get("session_id", ""),
                        category_count=context.get("category_count", 0),
                        redirect_fn=redirect_fn,
                        learn_fn=context.get("learn_fn", ()),
                        graph_fn=engram.graph_read_fn,
                        evaluation_time=context.get("evaluation_time", ""),
                    )
                    template_to_process = redirect_stmt.get("template", {}) or redirect_stmt.get("text", "")
                    new_context["response_state"] = response_state
                    redirect_response = engram.template_processor.process(template_to_process, new_context)
                    return redirect_response
        result = ""
        return result

    context["redirect_fn"] = redirect_fn

    def learn_fn(learn_data: dict) -> None:
        pattern = learn_data.get("pattern", "")
        template = learn_data.get("template", {})
        too_long = any(
            pattern_word_count(value) > MAX_PATTERN_WORDS
            for value in (pattern, learn_data.get("that", ""), learn_data.get("topic", ""))
            if value
        )
        with engram.statement_lock:
            shadows_shared = (
                bool(speaker)
                and bool(pattern)
                and engram.pattern_matcher.holds_category(pattern, learn_data.get("that", ""), learn_data.get("topic", ""))
            )
        if too_long:
            logger.info("Not learning a pattern longer than %d words", MAX_PATTERN_WORDS)
        elif shadows_shared:
            # Teaching never replaces a shared answer, even in the speaker's own chat.
            logger.info("Not learning a category whose path a shared category holds")
        elif pattern:
            # A statement keeps plain text as its text and structure as a
            # template object; a taught sequence becomes a sequence template.
            text = ""
            structured_template = {}
            if isinstance(template, dict):
                text = template.get("text", "")
                structured_template = template
            elif isinstance(template, list):
                structured_template = {"sequence": template}
            elif isinstance(template, str):
                text = template
            else:
                raise ValueError("a learned template must be an object, a sequence, or text")
            # An explicit teaching replaces what the speaker taught before on this path.
            engram.store(
                text=text,
                pattern=pattern,
                template=structured_template,
                that=learn_data.get("that", ""),
                topic=learn_data.get("topic", ""),
                tier=Tier.DYNAMIC,
                introduced_by_user_id=speaker,
                replace_learned=True,
            )

    context["learn_fn"] = learn_fn

    template_to_process = stmt.get("template", {}) or stmt.get("text", "")
    response = engram.template_processor.process(template_to_process, context)

    # Synchronize public predicates and remove template-local scratch keys.
    if session:
        with engram.session_lock:
            session_predicates = session.setdefault("predicates", {})
            for name, value in context.get("predicates", {}).items():
                if not name.startswith("_"):
                    session_predicates[name] = value
            leaked_scratch = [name for name in session_predicates if name.startswith("_")]
            for name in leaked_scratch:
                del session_predicates[name]

    return response

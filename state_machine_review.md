This is a strong design document — the most rigorous thing you've produced in this repo. It makes the right structural calls (three machines, pure reducer + shell, hand-rolled tables, no library, no schema bump), and it explicitly closes several of the gaps I flagged in earlier reviews. The review below is organized as: (A) what's genuinely good, (B) design-level issues worth resolving before Phase 0, (C) specific table/contract inconsistencies, and (D) answers to your six reviewer checkpoints.

A. What's genuinely good
The reducer/shell split is correct. The pure transition(state, event) -> (state, effects) with a transactional shell is the right shape, and the Effect enum makes side effects auditable. This is the thing that makes "deterministic replay" a claim you can actually test.

fold(events) == live
_state
 as a testable invariant is the single most valuable idea in the document. It converts "SQLite is authoritative" from an aspiration into a property with a test.

The compatibility phasing (shadow → dual-write → enforce → cleanup) per machine, gated by CHAOS_SM_* env flags, is the right incremental-rollout pattern. No big-bang.

The crash-action table (§6.5) is the best part of the document. It converts the scattered resume heuristics into a lookup, and it directly fixes the "crashed runs stay running" problem. Keep this.

The typed boundary classifiers (§5.3) — classify_diagnosis, classify_repair, classify_rep
air_result
 — correctly keep the pure machine independent of scenario JSON and string prefixes. This is what allows §5.5 to be a real table instead of pseudo-code.

The receipt model (§5.7) is now correct: deterministic approved:<run_id>, derived by fold, absent from payloads, redacted everywhere, wire value irrelevant after resume. This closes P0.3 cleanly.

§1.3 settles the fallback-comparability question I flagged in the previous review, and does it the right way: responder is provenance, not a score gate; chain identity preserved; per-backend baselines require explicit --backend <name> + CHAOS_LLM_FALLBACK=0. Good.

The non-goals list (§3.3) is disciplined and correct: no reset ladder, no topology epoch, no LLM loop, no export state, no health observer. This is the right scope.

§9.4's "shadow, compare across archived DBs, switch canonical only after parity" is exactly the right discipline for a scorer change.

B. Design-level issues to resolve before Phase 0
These are places where the design is internally consistent but I think the choice should be reconsidered or made more explicit before code exists.

B1. INTERRUPTED as "nonterminal, resume candidate, end reason required" is self-contradictory.
§6.2 lists INTERRUPTED with Terminal = no, resume candidate and End reason required = yes. §3.2 invariant 2 says "Terminal run states require end_reason." If INTERRUPTED is nonterminal, requiring an end reason violates the invariant's own framing; if it's terminal-for-now, then "resume candidate" needs a separate concept (a resumable flag, not terminality). This matters because §6.5's crash-action lookup keys off "state in interrupted run" and §6.4's ABORT transition from INTERRUPTED is unspecified (the table says "any nonterminal | ABORT | ABORTED", so it's covered, but the reader can't tell whether INTERRUPTED is in that set). Recommendation: add a third dimension to StateSpec — terminal: bool (true for FINISHED/ABORTED/SKIPPED_UNSUPPORTED), resumable: bool (true for INTERRUPTED and nonterminal states), and end_reason_required: bool (true for all terminal states and INTERRUPTED, because you need to know why it was interrupted to pick the crash action). That resolves the contradiction without changing the semantics.

B2. The END_REASON_REQUIRED on ESCALATED (gate) needs a companion rule for the run machine.
§5.2 says ESCALATED requires an end reason; §6.4 says AGENT_TERMINAL (gate escalation) transitions AGENT_RUNNING → ABORTED with a typed reason. So the gate's end reason becomes the run's end reason. But §9.2's strict_v3 only excludes "end_reason is not excluded" — it doesn't say which gate escalation reasons exclude from headline. diagnosis_attempts_exhausted should be visible but excluded from strict (it's a model failure, not an infra failure); budget_tokens likewise; premature_repair/forbidden_repair are policy violations, already excluded by the 
no violations
 clause. Recommendation: enumerate the gate escalation reasons in §5.5 and §9.2, and state for each whether it excludes from headline, strict, both, or neither. Right now the reader has to infer the mapping.

B3. The tool machine's UNKNOWN_EFFECT vs. the gate's receipt interaction is under-specified.
§7.4 says a mutating STARTED call that hits LEASE_EXPIRED becomes UNKNOWN_EFFECT, which "forces abort-and-retry." But §5.5 says APPROVED + a mutating repair can be in flight when the lease expires — and the gate machine's crash action for APPROVED is "resume; abort if mutating tool unknown." So the gate's crash action depends on the tool machine's state. That's fine, but the document doesn't say how the shell coordinates the two: does it fold the tool machine first, then the gate machine, then the run machine? If the tool machine says UNKNOWN_EFFECT, the gate machine should transition APPROVED → ESCALATED(reason=unknown_effect) rather than resume. §6.5 covers the run-machine side ("AGENT_RUNNING with mutating tool UNKNOWN_EFFECT → abort and retry"), but the gate-machine side is missing. Recommendation: add a "cross-machine coordination" subsection (probably in §8) that states the fold order and what each machine does when it observes another machine's state. The current document implies the coordination exists but never names it.

B4. The reaper's idempotency claim (§6.6) needs a concrete mechanism.
"Repeated detection of the same expired lease does not append duplicate transitions" is the right invariant, but §3.2 invariant 6 says "Every event is idempotent by (run_id, machine, seq)" — which is fine for events that carry a seq, but the reaper's REAP event is generated by the reaper, not by a seq-carrying producer. If the reaper runs twice before the shell processes the first REAP, you get two events with different seqs and the transition fires twice. The idempotency-by-seq rule protects against replaying the same event, not against producing the event twice. Recommendation: add an idempotency key to the REAP event (e.g., (run_id, from_state, lease_expires_at)), and have the shell check it before appending. State this explicitly in §6.6 and §4.4. Without it, the reaper is the one component that can violate invariant 6.

B5. state_transition_rejected vs. invariant 7 ("Illegal transitions append a rejection event and do not mutate state") is right, but §4.4's pseudo-code raises IllegalTransition after appending the rejection. That's fine, but it means the caller must catch it — and if the caller is the run loop, an illegal transition during a resume could abort a run that should have continued. Recommendation: state the contract: apply() raises on illegal transition after recording the rejection, and every caller (run loop, reaper, resume, shadow comparison) must handle it. For the run loop, an illegal transition should become ABORTED with reason state_machine_bug, not a silent continue. Add this to §4.4 and §6.4.

B6. The "no schema bump" claim (§10.1) is mostly true, but the new tool statuses are a real compatibility surface.
You note in §14 that "tool status consumers break on new strings." The mitigation is "update queries to accept both; doctor reports unknown values." But §7.2 lists UNKNOWN_EFFECT and SUPERSEDED as new persisted statuses, and §11.2 says doctor should check "unfinished mutating tool calls in UNKNOWN_EFFECT." If an old score.py or replay.py queries tool_calls.status IN ('finished', 'failed', 'denied'), it will silently miss the new states. Recommendation: either (a) keep the new states as internal aliases and persist failed with a result_code='unknown_effect', or (b) make the new statuses additive and audit every query in score.py, replay.py, sqlite_store.py that filters on tool status. I'd recommend (a) for the first release — it's the lowest-risk path and matches your §14 mitigation — and move to (b) in the cleanup phase.

B7. §1.3's fallback rule needs one more sentence about the provider_error_* end reason.
"Failures of earlier candidates are not a run-level provider_error_* when a later candidate answers" — good. But what is the end reason when all candidates fail? It's presumably provider_error_<last> or a generic provider_error_all, and the reader needs to know which, because §9.2's strict_v3 excludes provider-error runs. State the exact end-reason value in §1.3 and §6.3 (event payload). If it's a generic provider_error, say so; if it embeds the last candidate's error code, say that.

B8. The gate's CLEARED terminal state for no_fault is the right call, but §5.5's CLEARED + REPAIR_PROPOSED → CLEARED + violation means the gate never transitions to ESCALATED for a no-fault run that attempts repair. Is that intentional? A model that diagnoses no_fault, then attempts restart_member, should end up with a violation and the run should still be "clean" (the cluster was never broken). I think the design is right, but it means CLEARED can have violations, and §9.2's strict_v3 requires no violations — so a no-fault run with a blocked repair attempt fails strict, even though it correctly diagnosed. Is that intended? If so, say so; if not, §9.2 needs an exception for CLEARED runs where violations are all repair_after_no_fault. This is a real semantic question.

B9. The gate NUDGE event (§5.4) has no cap. §5.5 says AWAIT_DIAGNOSIS + NUDGE → AWAIT_DIAGNOSIS + emit nudge event. If the model gets nudged repeatedly without submitting a diagnosis, the loop emits unbounded nudge events. The current code has an early-nudge bug (P2.6) and a no-tool nudge, but the design should state a cap. Recommendation: add a n
udge_count
 to the gate state (or to DiagnosisFacts), and define a BUDGET_EXHAUSTED transition from AWAIT_DIAGNOSIS when nudge_count >= MAX_NUDGES. This is a small fix but it prevents an unbounded-event bug.

B10. §4.4's apply() returns State, not Transition. The reducer returns (state, effects), but the shell's 
apply()
 returns only State. That means callers can't inspect the effects (which they might need for logging, or for the cross-machine coordination in B3). Recommendation: apply() should return the Transition (state + effects + reason) so callers can log it, and the shell executes the effects internally. This is a small API change but it makes the shell's boundary honest.

C. Specific table and contract inconsistencies
C1. §5.2 vs. §5.5 on REPAIRED vs. CLEARED transition to ESCALATED. §5.2 marks both as terminal; §5.5's last row says "REPAIRED / CLEARED / ESCALATED | any | reject." So a budget exhaustion after a successful repair cannot transition to ESCALATED — it's rejected. That's fine (the repair is done, the run can move to verify), but it means the run machine must handle "gate is REPAIRED but the agent loop is still running." §6.4's AGENT_DONE transition expects gate_terminal in the payload, so this works, but the reader needs to know that REPAIRED/CLEARED are agent-loop-stop states, not run terminal states. §5.2 says "Gate-local terminal means the agent loop should stop" — good — but §5.5's rejection rule doesn't distinguish "rejected because terminal" from "rejected because the agent loop should stop." Consider renaming the column to agent_loop_terminal to disambiguate from run terminality.

C2. §5.5's AWAIT_DIAGNOSIS + ESCALATE/BUDGET_EXHAUSTED/ABORT → ESCALATED vs. §5.2's ESCALATED requiring an end reason. The transition table shows three events all going to ESCALATED, but only ESCALATE carries a reason in §5.4. BUDGET_EXHAUSTED carries a reason too, but ABORT also carries one. So all three have reasons — good — but the table doesn't show which reason becomes the end reason. If ESCALATE fires with reason=premature_repair, that becomes the end reason; if BUDGET_EXHAUSTED fires, budget_tokens becomes the end reason. Recommendation: add a reason column to the transition table for the → ESCALATED rows, and state the precedence rule (e.g., BUDGET_EXHAUSTED wins over ESCALATE if both fire, which they shouldn't). This is small but it's the kind of thing that produces a bug in Phase 1.

C3. §6.4's RESET_OK transition says "stop no renewal yet" — that's a typo or a note-to-self, not a table entry. The Effects column for RESETTING + RESET_OK → BASELINING reads "stop no renewal yet." I think you mean "stop the reset-phase renewal; baseline doesn't renew yet." Since §6.6 says setup renewal is being added, the effect for RESET_OK should be "renew lease through baseline" (or "continue renewal"), and the note about "no renewal yet" belongs in a design note, not the table.

C4. §6.2's PENDING crash action is "new run," but §6.4's none
+ CREATE → PENDING
 means a PENDING run has just been created and no work has started. A crash between CREATE and LEASE should arguably be a resume (nothing was done, but the run row exists and a batch may be in flight). The current table says "new run," which would create a second run row and orphan the first. Recommendation: change PENDING's crash action to "resume (re-lease)" — or, if you want to keep "new run," have the reaper mark the orphaned PENDING as ABORTED(reason=orphaned) before creating the replacement. Otherwise a crash-and-retry in PENDING produces duplicate rows.

C5. §7.4's INTENDED/STARTED + DENY
→ DENIED
 is ambiguous for a STARTED call. If execution has begun and then DENY arrives, you're denying a call that may already have side effects. The tool machine's DENY should only be valid from INTENDED (the gate denies before execution); STARTED + DENY should be rejected, not transition to DENIED. If you really want to support post-start denial, it should be STARTED + CANCEL → UNKNOWN_EFFECT (because you don't know whether the side effect landed). Recommendation: split the row: INTENDED + DENY → DENIED (clean), STARTED + DENY → reject (with a state_transition_rejected event). This matters because the whole point of the tool machine is to make unknown effects explicit.

C6. §8.3's legacy adapter maps "finished repair tool call" to gate.repair_finished, but doesn't say how it extracts the typed status. The adapter must map the legacy ERROR:/BLOCKED:/timeout waiting for strings to RepairResultFacts{status: success|failed|timeout|
denied}
 — that's §7.5's job, but §8.3 doesn't reference it. Recommendation: add a cross-reference from §8.3's row to §7.5, and state that the adapter uses the same classifier as the live path (so shadow comparison is apples-to-apples).

C7. §9.2's strict_v3 requires receipt_issued, but §5.7 says "the raw receipt is not stored in approval event payloads." So receipt_issued must be derived by fold from a correct diagnosis event, not read from a column. That's fine, but the GatePath dataclass (§9.1) carries receipt_issued: bool, so the question is: does the fold set it, or does the shell? Recommendation: state that 
receipt_issued
 is set by the fold when a DIAGNOSIS_SUBMITTED(correct) event is applied and a receipt_for(run_id) was issued; the 
GatePath
 is a derived artifact, not a projection. This is already implied by §9.1's "stored as a run_artifacts row," but the document should say it explicitly.

C8. §11.2's doctor checks mention "duplicate transition events for the same (run_id, machine, seq)" — but invariant 6 says every event is idempotent by that key. If duplicates are possible, the invariant is violated; if they're impossible, the doctor check is unnecessary. Recommendation: clarify whether "duplicate" means "two events with the same seq" (impossible, since seq is append-only) or "two events with the same semantic content" (possible, e.g., a reaper re-emitting 
REAP
). If the latter, the doctor check should be for "semantically duplicate transitions" and the mitigation is B4's idempotency key. Either way, §3.2 invariant 6 and §11.2 need to agree.

D. Answers to your six reviewer checkpoints
1. Is CLEARED the right terminal gate state for correct no_fault, or should it be NO_FAULT
_CONFIRMED
?
CLEARED
 is better, but for a different reason than naming: CLEARED describes the cluster state (clear of fault), while NO_FAULT_CONFIRMED describes the diagnosis. The gate's terminal states (REPAIRED, ESCALATED) are about outcome, and CLEARED fits that pattern. Keep CLEARED. However, the wire value cleared is ambiguous in logs ("cleared what?"). Consider no_fault_cleared as the wire value, with CLEARED as the enum name. That gives you both readability and consistency.

2. Should INTERRUPTED be parked nonterminal or terminal-with-forced-retry?
Parked nonterminal, as proposed, but fix the end_reason_required contradiction (B1). The reason to keep it nonterminal: a 
VERIFYING
 run that's interrupted can resume verify (read-only), which is strictly better than forcing a retry. A terminal-with-forced-retry model would lose that. The cost is that INTERRUPTED sits in queries for a while, which is fine — the reaper's job is to move it to a real terminal state once a human or the next run decides to abandon it. Add a --abandon operator path (or ./chaos abort --abandon) that moves INTERRUPTED → ABORTED(reason=abandoned) explicitly, so INTERRUPTED isn't a permanent limbo.

3. Is deriving the receipt from run_id acceptable given the current redaction model?
Yes, and it's the right call. The receipt is approved:<run_id>, which is already derivable from data the harness has, so storing it adds nothing. Redacting it from payloads is fine; deriving it from the fold is fine; accepting [REDACTED] after resume is fine. The one thing to double-check: if a run is resumed into a different process with a different run_id (retry), the receipt changes — which is correct (a retry is a new run). Make sure §5.7 says "the receipt is approved:<run_id> where run_id is the current run's ID," and that retries get a new receipt. If the current code passes the old receipt to the retry, that's a bug worth catching in Phase 1.

4. Is gate-path-v1 ready to become canonical after parity, or should it stay as an annotation scorer for one release?
Annotation for one release. The reason is not that gate-path-v1 is wrong — it's that the headline number is the thing readers compare across releases, and changing the scorer changes the headline even when behavior is identical. Run both for one release, publish both, and let readers see that strict_v3 and strict-loop-v2 agree on the archived set before switching. Your §9.4 already says this; I'm just endorsing it. The one addition: make the parity report public (in PHASES.md or 
docs/STATE_MACHINE.md
) so the switch is auditable.

5. Are the new tool statuses (unknown_
effect
, 
superseded
) compatible with downstream reports, or should they remain internal aliases?
Internal aliases for the first release (B6). Persist failed with a result_code column, and map to 
unknown_effect
/
superseded
 in the machine layer. Then migrate to first-class statuses in the cleanup phase once every query in score.py, replay.py, and sqlite
_store.py
 has been audited. This is the lowest-risk path and it matches your §14 mitigation ("keep legacy strings for old rows; update queries to accept both"). The downside is that the DB doesn't show the new states, which is a real cost — but the cost of a silently-skipped tool call in a report is worse.

6. Is the fallback comparability rule in §1.3 and §9.2 acceptable for headline reporting?
Yes, with one addition: the headline should be reported per chain configuration, not as a single number. §1.3 says "fallback-enabled runs are comparable as configured chains, not as clean per-backend baselines." Good. But §9.2's strict_v3 is a single boolean per run, and the score tables group by runs.backend (which now holds the chain identity, not the responder). So a reader sees "backend=auto, strict=0.20" and can't tell whether that's one chain or several. Recommendation: add a chain_id projection (a hash of the ordered endpoint list) and group the headline by (chain_id) rather than (backend). Then "auto" is a family of chains, each with its own headline. If you don't want that complexity, state explicitly in §9.2 that fallback-enabled runs are reported separately from per-backend baselines and are never aggregated with them.

E. Minor issues
§13 Phase 2 lists chaos_cli.py as a file to change, but the reaper's location is "to be chosen during implementation." A design document should name the location (e.g., harness/reaper.py, invoked by chaos_cli.py at startup and by ./chaos status). Pick one.

§12.7 says "No live test uses etcd-disk-full, per the standing exclusion." Good — but the standing exclusion isn't documented anywhere in this design or (as far as I can see) in the README. Add a one-line rationale (destructive to the point of unrecoverable?) and a pointer to where it's enforced.

§16's Mermaid diagram shows AWAIT_DIAGNOSIS --> AWAIT_DIAGNOSIS: wrong diagnosis / nudge / premature repair, which conflates three different events with different payloads and different effects. The generator (§4.6) will produce per-event edges, so the hand-drawn diagram will be replaced — good — but the hand-drawn one is currently misleading. Consider making it illustrative-only with a note, or generating it from the start.

§17's diagram shows VERIFYING --> INTERRUPTED: LEASE_EXPIRED, which is correct per §6.4, but it also shows VERIFYING --> FINISHED: VERIFY_OK / VERIFY_FAILED without noting that both require a score row and final record. The diagram is illustrative, but the transition table (§6.4) should say "write score and final record" for both.

§19's glossary defines "Shadow mode" as "Reducer runs without controlling behavior; mismatches are logged." Good. But the document uses "shadow comparison" in §12.2 and "shadow adapter" in §8.3 without defining whether those are the same thing. They're not: shadow comparison is the test, shadow adapter is the legacy-event mapper. Add both to the glossary.

F. One structural suggestion
The document is 19 sections and ~700 lines. That's appropriate for a design proposal, but it means reviewers will skim. Consider adding a one-page "decision summary" at the top (above §0 or replacing most of §1) with the 8–10 decisions a reviewer must approve: three machines, pure reducer + shell, no schema bump, receipt from run_id, INTERRUPTED parked-nonterminal, fallback responder-is-provenance, tool statuses as internal aliases in v1, gate-path-v1 annotation-only for one release, setup lease renewal before reaper, no reset-ladder/topology/health machines. Each decision gets one line and a pointer to the section that justifies it. That's the artifact that gets you a decision, and the rest of the document is the defense.

Bottom line
This is a publishable design document, and the Phase 0 → 4 sequence is the right order. Before Phase 0, resolve B1 (INTERRUPTED terminality contradiction), B3 (cross-machine coordination order), B4 (reaper idempotency key), B6 (tool status compatibility), and C4 (PENDING crash action) — those are the ones that will produce real bugs in Phase 1–2 if left implicit. B8 (no-fault-with-violation strictness) and B2 (escalation-reason enum) are semantic decisions you should make now, because they affect strict_v3 and therefore the headline number. Everything else is polish or documentation, and the reviewer checkpoints are well-chosen: your six questions are exactly the six I'd ask, and the answers are mostly already implied by the document — they just need to be stated as decisions rather than left for the reader to infer.


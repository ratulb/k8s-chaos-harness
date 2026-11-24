This revision resolves every publication-blocking item from my previous review and adds the decision-summary page I suggested. The design is now coherent enough to start Phase 0. Below is a focused review: what's resolved, what remains, and the handful of items I'd still tighten before code exists.

What's resolved
All ten of my prior recommendations are addressed in the document, and several are improved beyond what I asked for:

B1 (INTERRUPTED contradiction). §4.2 now has explicit terminal, resumable, end_re
ason_required
 dimensions, and
StateSpec
 carries all three. §6.2 and §6.5 key the crash action off previous_state, not off INTERRUPTED. §6.6 adds --abandon. This is clean.

B2 (escalation-reason enum). §5.5.1 is exactly the table I asked for, with strict/headline/funnel columns per reason. The "excluded numerator, visible denominator" rule for run-level reasons is the right generalization.

B3 (cross-machine coordination). §8.4 names the fold order (tool → gate → run), names the three inter-machine rules (unknown mutating effect escalates outward; gate terminal feeds run; superseded read-only is a no-op), and states the shell executes coordination after all folds agree. This closes the gap.

B4 (reaper idempotency). §3.2 invariant 6 now distinguishes producer-seq idempotency from generated-event idempotency, and §6.6 specifies idempotency_key = "reap:{run_id}:{previous_state}:{lease_expires_at}" with a same-transaction check. §11.2's doctor check is now "semantically duplicate generated transitions with the same idempotency_key," which is the right wording.

B5 (illegal transition contract). §4.4's caller contract — run loop aborts with state_machine_bug, reaper logs and skips, resume fails closed, shadow logs and continues with a shadow record — is exactly the discipline needed. §6.4's "An ILLEGAL_TRANSITION reaching the run machine is always a harness bug" makes this explicit.

B6 (tool status compatibility). §7.2's v1-persisted-status + result.code scheme is the right lowest-risk answer, and §14's mitigation row matches it. This avoids silently skipping rows in old score.py/replay.py queries.

B7 (all-candidate failure). §1.3 pins provider_error_all as the sole reason, no provider_error_<last> variant, and the ordered attempts stay in the payload. §6.3's PROVIDER_ERROR_ALL event matches.

B8 (no-fault-with-violation strictness). §5.6 and §9.2 now state explicitly that a control run that attempts a repair stays CLEARED but fails strict via repair_after_no_fault. §15 item 7 records this as a decision. This is the right call and it's now visible.

B9 (nudge cap). §5.4 has MAX_NUDGES = 2 and nudge_count in gate state; §5.5 has the BUDGET_EXHAUSTED transition when the cap is reached. Resolved.

B10 (
apply()
 return type). §4.4 now returns Transition, and the shell executes effects internally. Good.

The specific table inconsistencies I raised are also fixed:

C1 — §5.2 uses "Agent-loop terminal" as a column, disambiguated from run terminality, with a clarifying sentence. Resolved.

C2 — §5.5 has an
End reason
 column, and the precedence rule BUDGET_EXHAUSTED > ESCALATE > ABORT is stated. Resolved.

C3 — §6.4's RESET_OK effect is now "continue lease renewal through baseline," and the "no renewal yet" note is gone. Resolved.

C4 — §6.2's PENDING crash action is now "resume (re-lease)," with §6.5's rationale ("a new row would orphan the existing run"). Resolved.

C5 — §7.4 splits INTENDED + DENY (clean) from STARTED + DENY (reject), and adds STARTED + CANCEL → UNKNOWN_EFFECT for mutating calls. Resolved.

C6 — §8.3's "finished repair tool call" row now says "typed status extracted by the same classifier as §7.5." Resolved.

C7 — §9.1 states receipt_issued is fold-set and GatePath is a derived artifact, not a projection. Resolved.

C8 — §3.2 invariant 6 and §11.2 now agree on the two kinds of idempotency (producer seq vs. generated idempotency_key). Resolved.

The decision summary table at the top is a good addition — it's the one-page artifact a reviewer can approve or reject, and each row points to the section that defends it. The glossary additions (shadow comparison vs. shadow adapter vs. idempotency key vs. internal alias vs. chain ID) fix the §19 gaps I flagged.

What I'd still tighten before Phase 0
T1. §6.2's INTERRUPTED crash action is "per previous state," but §6.5's lookup table keys off previous_state — and §6.4's LEASE_EXPIRED transition from "any nonterminal" includes INTERRUPTED itself. A LEASE_EXPIRED event arriving on a run already in INTERRUPTED (say, the reaper runs twice, or a second process crashes) should be a no-op or a rejection, not a re-entry into INTERRUPTED. The table currently permits it. Fix: exclude INTERRUPTED from the "any nonterminal → LEASE_EXPIRED → INTERRUPTED" row, or add an explicit INTERRUPTED + LEASE_EXPIRED → INTERRUPTED (idempotent, no new event) row with the idempotency key as the guard. This is the one place where the reaper's own idempotency and the run machine's transition table aren't in agreement.

T2. §5.5's AWAIT_DIAGNOSIS + NUDGE | BUDGET_EXHAUSTED | nudge cap reached → ESCALATED conflates two different events with two different causes. The NUDGE event that increments nudge_count is in the previous row; the cap-reached case is a guard on the same event, not a separate event. Splitting them as NUDGE (below cap) and NUDGE / BUDGET_EXHAUSTED (cap reached) means the reader has to infer that NUDGE at cap maps to BUDGET_EXHAUSTED(reason=budget_tokens). Fix: either (a) make the second row NUDGE with guard "cap reached" and effect "emit BUDGET_EXHAUSTED then transition," or (b) rename the second row's event to BUDGET_EXHAUSTED only and move the cap-reached→BUDGET_EXHAUSTED conversion into the nudge-cap rule in §5.4. Right now the table and the rule are saying the same thing in two places, which is a drift risk.

T3. §8.4's coordination rule "tool SUPERSEDED on a read-only call → no gate or run transition" is correct, but it doesn't say what happens if the superseded call was a REPAIR_PROPOSED that never finished. A read-only tool call being superseded is fine; a repair tool call being superseded (if that's even possible — repairs are mutating, so they should be UNKNOWN_EFFECT not SUPERSEDED) is a different matter. Fix: add a sentence: "Repairs are mutating; a repair call that hits RESUME_SUPERSEDE is a bug — the tool machine rejects it, and the shell records a state_transition_rejected event." This closes the last ambiguity in the cross-machine rules.

T4. §9.2's strict_v3 requires end_reason in {verify_ok}, which is a strong claim: every strict-success run must end with the literal verify_ok end reason. That's consistent with §6.4 (VERIFY_OK → FINISHED with verify_ok), but it means VERIFY_FAILED → FINISHED(verify_failed) can never be strict, which is right, and it means any future end reason that's also a "success" (e.g., no_fault_verified if you ever separate control runs) would fail strict by default. Fix: either keep the set literal (and document that adding new strict-eligible end reasons requires updating §9.2), or express it as end_reason not in EXCLUDED_REASONS where EXCLUDED_REASONS is the §5.5.1 + §9.2 union. The second is safer against future drift; the first is more auditable. Pick one and say why.

T5. §12.1's exhaustive-table-test requirement ("every (state, event) pair yields either a defined transition or a defined rejection") is the right test, but the transition tables don't currently enumerate rejections explicitly — they say "terminal | any | reject" and rely on the test to catch missing rows. The design would be stronger if the generator (or a test helper) emitted the full state × event matrix from the tables and failed if any cell was neither an explicit transition nor an explicit reject. Fix: add one sentence to §12.1: "The test loads the transition tables and asserts every (state, event) cell is either an explicit row or an explicit reject; a missing cell is a test failure, not a runtime IllegalTransition." This makes "illegal transitions are visible" (a goal in §3.1) testable rather than aspirational.

T6. §13 Phase 2's file list includes harness/reaper.py and chaos abort --abandon, but run_benchmark.py is listed without saying what changes there. Phase 2's acceptance criteria ("phase writes only in transition shell," "setup lease renewal active") imply run_benchmark.py is being rewritten to call the shell instead of writing runs.status/runs.phase directly. Fix: add a sentence to Phase 2's acceptance: "run_benchmark.py no longer writes status, phase, or end_reason directly; all writes go through apply()." That's the verifiable condition.

T7. §13 Phase 3 doesn't mention the result.code migration. §7.2 says new states are persisted as finished/failed + result.code in the existing result TEXT column as JSON. But existing rows may have plain-text result values (like "superseded_after_resume" from run_benchmark.py:288-298, per §2.3). Fix: add a sentence to Phase 3's acceptance: "Existing plain-text result values are read as
result.code
 only when they're JSON; otherwise they're treated as a plain result.text and preserved. New writes always use the JSON envelope." This is the readability contract that makes "old rows remain readable" true.

T8. §15's resolved-checkpoints list has eight items, but the six original reviewer questions plus B8 (no-fault strictness) and B2 (escalation-reason enum) — that's eight, matching — but the document doesn't say which of the six original questions are now decisions vs. open. The header says "resolved as decisions; two semantic questions from the review are also recorded here for auditability." That's clear enough, but a reader scanning for "what's still open" won't find it. Fix: add a one-line note at the end of §15: "No reviewer checkpoint remains open; any future disagreement is a new proposal, not a reopened question." That's the closure statement that makes the decision summary at the top authoritative.

One small addition I'd recommend
Add a "§0.1 — What this design does not fix" section. The non-goals (§3.3) cover the scope exclusions (no reset ladder, no LLM-loop machine, etc.), but they don't cover the known gaps this design leaves in place. Specifically, the design resolves P0.3, P1.5, P1.7, P1.12, P2.6, run crash state, resume ambiguity, and tool unknown effect (per §2.5), but it does not address:

P1.4 (three definitions of success — common/scoring.py, score.py, replay.py still compute independently; gate-path-v1 is one of them, but the unification is Phase 4/cleanup).

P1.6 (trace written twice — engine appends and export_all() rewrites; the state machine doesn't change this).

P1.8 (tools/ mixes domain and infrastructure).

P1.9 (primitive-obsession leftovers — some are resolved by typed events, but
config_hash
, Backend, call_id remain).

P1.10 (destructive policy scattered — the gate machine owns gate policy, but
safety.py
, topology.py:capability, and prompt.py still each hold a piece).

P1.14 (whole-flow testability — run_benchmark.main() and chaos_cli.main() still mix argv/exit/subprocess; Phase 2 changes what they call but not their shape).

P2.1 (streaming fallback invisibility — unrelated to machines).

P2.5 (score-version precedence — gate-path-v1 is a new version, but the read-side ORDER BY ambiguity is not addressed).

A short section listing these — "Known gaps not addressed by this design; tracked in ARCHITECTURE.md §5" — prevents a reviewer from concluding the state-machine work is the whole refactor. It isn't, and the document should say so plainly. This is the one structural omission I'd flag as worth adding before Phase 0.

Bottom line
The design is ready for Phase 0. T1 (the INTERRUPTED + LEASE_EXPIRED table row) is the only item I'd call a correctness fix rather than a clarity fix; the rest (T2–T8) are precision improvements that will make Phase 1–2 code easier to write and review, but none blocks starting. The decision summary, the resolved-checkpoints section, and the expanded glossary make the document approvable as-is; the "what this design does not fix" section is the one addition I'd argue for strongly, because it's the difference between "this is the refactor" and "this is one refactor among several," and the document currently reads like the former.


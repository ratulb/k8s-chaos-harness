https://github.com/ratulb/k8s-chaos-harness is public now - have a detailed look. I am going to make it private again.
This is the published version of the repo — and it's noticeably ahead of the README you last sent me. The commit log shows work through Nov 23, and several of the things I flagged as "still open" have moved. Here's what changed, what I'd still fix, and what actually matters if the goal is to make it private again soon.

What's new since your last README
The biggest change is that the LLM backend story is now much larger than the "one backend per run, no fallback chain" framing I reviewed. §7 now describes:

--backend auto (highest-priority configured endpoint first), explicit fallback chains via CHAOS_LLM_ENDPOINTS, CHAOS_LLM_FALLBACK, CHAOS_LLM_MAX_ATTEMPTS, CHAOS_LLM_FALLBACK_RETRIES, CHAOS_LLM_HEALTH_SORT, and health-aware ordering.

Provenance: the backend that actually answered is recorded in llm_requests, messages, and llm_response_full; if all candidates fail, llm_error events and llm_error_full artifacts list every attempt.

config_hash records the selector and name/model chain, so resume can't silently switch policy.

That's a real change in design, not just docs. It has one immediate consequence you should decide on before going private: the README still says in §1 and §2 "there is no fallback chain" — wait, no, the published §1 now says "Comparable backends with explicit provenance … auto and configured fallback chains keep runs alive when one endpoint fails." So §1 has been updated. But check §14's scoring: "A run is headline-eligible iff … end_reason is not legacy_exploratory or a provider-error or unsupported end reason." With fallback chains, a run that succeeds after one provider failed is now headline-eligible, and its provenance is recorded — but the README does not say whether a run whose primary failed and whose fallback answered is (a) counted normally with provenance noted, (b) annotated as fallback in the score, or (c) excluded from cross-backend comparability. This is the single most important design question the new feature creates, and it's not answered anywhere in the README.

What got fixed from my earlier review
Several of the publication blockers are resolved in this version:

A5 (alias policy divergence). §6 now says aliases are display-only, canonicalize before policy, and "an alias can never get different treatment from its target." etcd-disk-full lists member_remove_readd (canonical) rather than the alias. §11 mirrors this with a canonicalization step in the gate order. Good.

A3 (token → gate receipt). Renamed throughout, with a clear threat-model paragraph in §11 ("a correctness handshake, not a secret or human authorization … a correct label does not authorize an unsafe repair"). This is the right framing.

A1 (fault-injection semantics). §10 step 9 now splits injection_failed (command failure) from fault_not_observed (stimulus not proven present), both excluded from headline and strict. §15 agrees. Consistent.

A6 (headline metric drift). §1 leads with strict_loop_success = 0.20 and explicitly demotes the 46.2% legacy number.

P0 fixes. §17 says "The P0 verify-env, fixture-env, lease, shell-validation, and gate-receipt gaps have been addressed." Commit Harden event redaction and lease renewal suggests P0.4/P0.5 landed; Neutralize public references and Close CLI batch-id and scenario quoting gaps suggest P1.15 and P2.4 landed. That matches my recommended sequence (fix P0 before refactor).

What's still wrong or newly wrong
§13 table contradicts §10 and §15 on stale-state behavior. This is the same issue I flagged last time, still unfixed. §13's first row says "normal ./chaos run … Loudly discards stale state, then exits." §10 step 5 and §15 say it now archives under archive/<timestamp>/ and requires --discard-stale. The §16 symptom table also says "archived stale state." So three sections say archive, one says discard. Fix the §13 row.

--discard-stale is documented in §10 step 5, §15, and §16 but not in §9's ./chaos run options table. Add it. Also, the CLI in §9's command list doesn't show it — if it's accepted by ./chaos run, the command synopsis should show it.

injection_failed is not in §14's <details> end_reason list. The list enumerates submitted, budget_tokens, agent_stopped, fault_not_observed, reset_failed, baseline_failed, operator_abort, provider_error_* — but not injection_failed or skipped_unsupported. The §4 Core concepts table added injection_failed and legacy_exploratory, but the full enum still lives in ≥3 partial lists. This was my chunk-2 issue #6 / A8; still open.

§9's run-options table says --backend <name> is required, but §7 says --backend auto is the default and the command synopsis shows --backend ollama|groq|openrouter|cloudflare. With custom endpoint names and auto now valid, the option table and synopsis are stale. Update to --backend <name|auto> and mention CHAOS_LLM_ENDPOINTS names.

./chaos ping is new in §8/§9/§7 but has no <details> block in the CLI operation reference. Every other command has a five-line what/expects/does/evidence/failure-modes block. Ping is the first command a new user runs and it has no reference entry. Add one.

§14's end_reason and scoring sections do not mention fallback chains. Now that runs can succeed via a fallback endpoint, the scoring section should state whether the answered-on-fallback case is headline-eligible and how it's annotated. This is the design gap I flagged above; it should be resolved before the full sweep.

§17's "P0 gaps have been addressed" is a strong claim with no per-item status. The commits suggest this is true, but a reviewer checking ARCH §5 against this sentence will want each P0 item marked. Better: "P0.1 (verify env), P0.2 (fixture env), P0.4 (lease), P0.6 (shell validation), P0.7 (gate receipt) addressed; P0.3 (receipt persistence) addressed with redaction in state snapshots; P0.5 (redaction asymmetry) addressed at the event/artifact layer, raw DB fields can still retain values." Either this or move the claim into PHASES.md with per-item status and have §17 defer.

§7's fallback model has an unstated interaction with --seed and temperature. Temperature is fixed at 0.2 and --seed is metadata. If auto picks a different backend across runs of the same scenario, the seed is "recorded" but not controlled, and temperature is fixed — fine. But cross-backend comparability now has three dimensions: (a) fixed scenario, (b) fixed temperature, (c) fallback chain configuration. The README doesn't say what a "comparable baseline" means when fallback is on. If the intended headline is per-backend, fallback must be off for headline runs; if the intended headline is per-chain, the chain is the unit. Pick one, say it in §7 and §14.

§12's artifact table still mixes files and DB tables (my chunk-2 issue #11). Not blocking, but it's the kind of inconsistency a reviewer trips over.

§12 says "Legacy backend aliases normalize to ollama on DB init and export." With auto and custom names in the mix, this sentence now needs a companion: what happens to a run whose llm_requests rows show auto selected groq? Does export preserve the answered backend or normalize it? The config_hash records the chain; the llm_response_full records the answerer; but the scalar runs.model field's behavior isn't stated. If it stays auto, per-backend analysis breaks; if it becomes groq, the chain is lost. Pick one and say so.

The documentation map lost docs/archive/feedback.md, docs/archive/SQLite.md, etc. The commit Salvage store details and retire historical docs suggests these were removed. If so, §18's "When documents conflict … DESIGN.md and PHASES.md win" is now a two-document authority claim without the archive it was defending against. Fine, but say it explicitly ("historical docs retired at commit X") so a reader who finds old links knows what happened.

docs/ARCHITECTURE.md is now "commit-pinned" at §6, but the README doesn't say what commit. §17 pins the README review at 1952bb2. ARCH's header (from your earlier paste) said "commit 134d2e9." These no longer match, and neither matches the HEAD commit (Close CLI batch-id…, Nov 23). Either pin both to the same tag/commit, or say "ARCH §6 describes commit X; README §17 describes commit Y; both may lag current code."

What I'd do before making it private again
If the goal is "make it private soon, with the docs in a defensible state," the publication-blocking fixes are smaller than the full review list:

Fix §13's stale-state row (archive, not discard) — one line.

Add --discard-stale, injection_failed, skipped_unsupported to the places they're missing (§9 table, §14 enum) — five lines.

Add a ./chaos ping <details> block — five lines, and it's the first command a new user runs.

Resolve the fallback-eligibility question in §7/§14: is a run whose primary failed and whose fallback answered (a) headline-eligible with provenance, (b) annotated and excluded from per-backend comparison, or (c) excluded? This is the only design question the new feature opened, and it should be answered before PHASES.md or ARCH claim the full sweep is pending rather than undefined.

Reword §17's "P0 gaps addressed" to per-item status, or move the claim into PHASES.md.

Update §9's --backend synopsis to include auto and custom names.

That's a half-day of docs work and it closes every inconsistency the published version actually contains. The rest of my earlier review items (one Score object, SQLite-only derived writes, streaming-fallback visibility, Scenario authoritative, ExperimentSpec, RunStore) are refactor work, correctly deferred per §17 and ARCH §7 — none of them is a reason to keep the repo private.

One thing worth naming: the published repo now has stronger claims than the README it ships with (fallback chains, provenance, health-aware ordering) and the README's two scoring tables still don't account for them. If the repo goes private before the fallback-eligibility decision, that decision will be made later by whoever runs the sweep, and it will be made implicitly. Better to make it explicit now, in §7 and §14, even if the implementation is already in llm/ and common/
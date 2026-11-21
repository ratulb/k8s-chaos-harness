This is a genuinely strong README — it reads like the doc of a project that has been through real operational pain, and that's exactly the right voice for a chaos benchmark. Before you go public, here's a candid review: what lands well, what to tighten, and specific fixes.

What lands well (keep it)
Section 1 "Why this exists" is excellent. The one-sentence question ("can a model find the correct root cause... and choose an acceptable, safe repair?") instantly tells a visitor what's being measured. Most benchmark READMEs never do this.

The honesty in §17 is rare and valuable. Listing non-implemented commands, known undercounting, redaction caveats, and "repair-mode semantics don't match design" builds more trust than hiding it. Keep every word.

§9's "Not implemented" note and §5's fallback disclaimer ("setup.conf is a rebuild recipe... not an override") are the kind of corrections that save a user hours.

§10 "One run, end to end" + the state diagram is the single best section for orientation. Keep it near the top of the reading order.

Quorum table (§5) and scenario/repair tables (§6) are exactly the right way to present this — scannable, verifiable, no prose.

§16 troubleshooting table is immediately useful.

The README's main virtue: it tells you what is not true, not just what is.

Highest-impact fixes before public
1. Add a "What you'd need" / prerequisites block near the top
Right now prerequisites live in §8. A visitor deciding whether to clone needs: Linux, Python 3, kubectl, SSH to control planes, an existing 3-node cluster, a backend key. Put a 4-line "Requirements at a glance" box right after §1. Many readers will bounce if they only discover the cluster requirement in §8.

2. Lead with the headline metric and how to read it
loop_success is defined in §14 but never shown as a result. Add a short "What good looks like" paragraph and, once you have numbers, a results table (scenario × backend ? diagnosis %, verify %, loop_success). The §17 note that the full sweep is pending is fine — but a "Results" section with a placeholder ("full cross-backend sweep pending; reduced 13-run sweep results in PHASES.md") signals intent and directs readers.

3. Fix the kaggle / kaggle-ollama normalization contradiction
§2 says "kaggle is not a backend name." §12 says "kaggle and kaggle-ollama normalize to ollama on DB init and export." Both can be true (it's not a selectable backend, but legacy values are coerced), but as written it reads like a contradiction. Reword §2 to: "kaggle is not a selectable backend; legacy kaggle/kaggle-ollama values in the DB normalize to ollama (§12)."

4. Define legacy_exploratory where it's used
§14 says headline records exclude "no legacy_exploratory end reason." A reader has no idea what that is. Add one clause: what sets it and why it's excluded. Same for reset
_failed
/
baseline_failed
 — they appear in §10 and §16 but are only explained in the troubleshooting table. A one-line glossary pointer would help.

5. Move the "commands are the source of truth" caveat up
§17 says "current code plus DESIGN.md and PHASES.md win" over historical docs. That's the right rule, but it's buried at the very bottom. Add a short "Authority" note near the top: "If anything conflicts, current code and DESIGN.md win; PHASES.md is status." You already say something similar in the intro — make it a one-line callout so people don't over-trust the historical docs.

6. §1 bullet "Comparable backends" — clarify the baseline question
"Same scenarios run against ollama, groq, openrouter, cloudflare" is good, but a visitor will ask: is ollama the reference baseline, or just one of four? §7 says "no fallback chain; backends are comparable baselines." That's the answer — fold that phrasing into §1 so the claim is consistent.

Medium-impact polish
§3 architecture diagram: the ASCII flow is readable, but the -> LLM backend -> tool registry line conflates two layers (backend vs. tools). Consider splitting into a runner column and a side effects column, or add a one-line legend under the diagram ("left: orchestration; right: external systems"). Minor.

§9 --mode row: "Benchmark/repair; policy/prompt approval" is terse. Expand to "benchmark (default) or repair (best-effort incident context; see §9 caveat)."

§11 tool list: the diagnostic tools are a comma-separated run-on. Break into a bulleted list or a small table with "what it shows" — it's the section a user integrating a new tool will reread most.

§11 "about 4000 characters per tool result": is that a hard cap or approximate? Say "˜4000 chars (truncated)" so people aren't surprised.

§12 table: traces/*.jsonl appears three times with different suffixes. A single "Artifacts" list with a one-line "derived vs. authoritative" column would reduce duplication. (You do say "never edit as source of truth" — keep that.)

§13 state lifecycle: excellent table. Add the missing case: what happens to logs/ and traces/ when disc
ard
 runs? You say "logs/ survives" — state explicitly whether traces are deleted (they are, per §13 "deletes traces/*.jsonl"). One line saves a surprise.

§14: "streamed responses report zero token usage, so totals can be undercounted" appears in §17 too. Pick one home (I'd keep it in §17 limitations and add a pointer in §14) to avoid drift.

§16 test commands: good. Add a note that CHAOS_LIVE_BACKEND_TESTS=1 requires a reachable backend and will spend tokens — a caution people appreciate.

§17 "As of 2026-11-21": good practice. Consider also putting the current git commit or version tag there, so the status is pinned to a state of the repo.

§18 doc map: strong. Add a "Start here" arrow (README ? quickstart ? one run) since nine docs can be intimidating.

Small copy edits
§2: "It is not ... a fixed three-node tool" — good, but "three etcd members are the loud recommendation" in §5 slightly undercuts it. They're consistent (recommendation ? hard requirement), but a reader may trip. Add "recommended, not required" in §5.

§5 table row 1 | 1 | 1, benchmark-ineligible total outage — the trailing phrase is awkward. Reword: "1 (benchmark-ineligible: total outage)".

§10 step 8: "aborted with reset_failed or baseline_failed" — these are end reasons, correct, but readers may not know they're enums. The troubleshooting table covers them; a cross-reference "(see §16)" helps.

§14 "loop_success means correct diagnosis, verify pass, and repair presence matching fault status; it does not require an acceptable repair or absence of forbidden actions." — this is important and slightly counterintuitive. Consider bolding the second clause or making it a callout; people will misread the metric otherwise.

§15 "Reset failure is an infrastructure failure, not an agent failure." — excellent line. Consider promoting it to a callout in §10 too, since it's a common misattribution.

Mixed ./chaos vs python3 ... command styles are fine, but add one line in §9: "./chaos is the intended entry point; python3 ... commands are the underlying tools it wraps."

Structure suggestion (optional, but I'd do it)
The README is long (~500 lines). Consider a table of contents right after the intro with anchor links to §1–§18. For a doc this dense, a TOC is high-value. GitHub auto-generates anchors from headers, so it's cheap to add.

Also consider pulling §15 Operations and safety and §16 Troubleshooting into the TOC with explicit "?? destructive" markers so the safety-critical sections are findable from the top.

One thing to decide
Your §17 is candid that ./chaos doctor|score|export and --reference-only are not implemented. For a public repo, two options:

Keep as-is (my preference) — the honesty is a feature, and it preempts "this is broken" issues. Just make sure the issue template asks "did you check §17?"

Add a ROADMAP.md and move the "not implemented" list there, leaving §17 as "see ROADMAP." This keeps §17 shorter but loses the in-context warning. I'd keep it inline and also add a ROADMAP pointer.

Suggested small patch set (copy-ready)
If you want, I can produce a concrete diff for these edits:

Insert a "Requirements at a glance" box after §1.

Insert a "How to read this / Authority" callout at the top.

Reword the kaggle sentence in §2 to match §12.

Add a one-line glossary for legacy_exploratory, reset_failed, baseline_failed (or link to §16).

Add a TOC after the intro.

Bullet-ize the §11 tool list.

Add a "Results (pending)" stub section after §14 with a pointer to PHASES.md.

Bold/callout the loop_success caveat.

Add the traces/ deletion behavior to §13's discard row.

Tell me which of these you want and I'll write the exact markdown so you can paste it in. Also: since the repo isn't public yet, want me to suggest a repo description, topics/tags, and a short GitHub About blurb to go with it? Those are cheap to get right now and painful to change later.


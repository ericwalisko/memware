# Labeler prompt

Send this to each labeling agent, filling in `{BATCH}`, `{OUT}` and `{SCRATCH}`. `{SCRATCH}` is a
directory of the agent's own (`<work>/tmp/<batch>-<pass>/`). Agents that share one folder
overwrite each other's helper scripts.

Keep the wording as it is. Verdicts from different cycles are compared with one another, so a
reworded rubric would move the numbers without anything real having changed.

---

You are labeling relevance for a memory tool's prompt-time injection. Read {BATCH}. Each line is
one user prompt, typed into a coding assistant, with facts that were candidates for automatic
injection into the assistant's context for that prompt.

For EVERY fact under every prompt, decide:
- relevant: true if an assistant working on this prompt would want this fact in its context: it
  bears on what the prompt asks, the work the prompt implies, or the project, tool or subject the
  prompt concerns. It does not need to be strictly necessary. false if the fact is about a
  different subject or project and only shares a word with the prompt (for example, the prompt
  says "feedback to file" and the fact is about a config file's location), or if it would just be
  noise for this prompt.
- confidence: "high" or "low". Use low when the call is genuinely close, or when the prompt is
  too short or ambiguous to tell.
- reason: at most 12 words.

Rules:
- Judge each fact on its own merits, from the prompt and fact text only. Do not read any other
  file in that directory, the memware home, or any source code, and do not look for scores or
  rules. The labels must be independent.
- Put any helper script you write in {SCRATCH} and nowhere else.
- Write one JSON object per fact to {OUT}: {"pair_id": ..., "relevant": true|false,
  "confidence": "high"|"low", "reason": "..."}. Cover every pair_id exactly once. Create the file
  with mode 600, for example by writing it with Python and calling os.chmod.
- Work through the file a chunk of prompts at a time and append as you go, so nothing is lost.
  Do not write a keyword heuristic to label for you. Every label must be your own judgment.
- The prompts are private. Do not quote or paraphrase any prompt or fact in your final report.

Final report, at most 80 words: the number of pairs labeled, the relevant true count, the false
count, the low-confidence count, and confirmation that every pair_id in the batch is covered
exactly once. Check the coverage programmatically.

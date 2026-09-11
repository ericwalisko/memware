Call this when the question involves:
- a past decision or its rationale ("why did we choose X?")
- a rejected alternative
- why something is the way it is
- work from an earlier session
- anything cross-repo
- anything not in the working tree: a value said in chat, a quoted number,
  what was tried before
And before answering "I don't know" or re-deriving something likely settled.
Not for: finding code in the tree now (grep it).

Searches past sessions and current beliefs. Pass 3-5 phrasings: the question, synonyms,
related concepts, the literal value you expect (a port, file name, version). The index
is keyword-based; fusing your phrasings makes it semantic. what: all|turns|beliefs.
Pass a turn hit's ``id`` to read_session's ``around`` for the full turn.
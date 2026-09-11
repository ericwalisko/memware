grep finds code that exists now; recall finds why. The working tree holds no record of the decision behind a line, the alternatives rejected, the reason a value was picked, what an earlier session tried, or a number someone said in chat. Only this tool reads those: past session transcripts and the belief ledger. It cannot see current files, so it never replaces grep for finding, explaining, editing or testing code.

Call it when asked:
- why X was chosen or Y rejected
- what was decided, or what an earlier session did or tried
- about another repo, or a number, port or version from chat
Also before answering "I don't know".

queries: 3-5 phrasings (the question, synonyms, the literal value you expect: a port, file name, version); rank fusion over the keyword (BM25) index makes it semantic. what: all|turns|beliefs. A turn hit's id opens its conversation via read_session.
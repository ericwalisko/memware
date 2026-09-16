grep finds code that exists now; recall finds why it exists. The working tree holds no record
of the decision behind a line, the options rejected, the reason a value was picked, what an
earlier session tried, or a number someone said in chat. Those live only in past session
transcripts and the belief ledger, and only this tool reads them. It is blind to the working
tree, so it never replaces grep for locating code.

Call it before grepping when asked:
- why a value, port, version or approach was chosen or rejected
- what an earlier session did, tried or concluded
- anything cross-repo, or said rather than committed
- anything the tree states without justifying
Also before answering "I don't know" or re-deriving something likely settled.

queries: 3-5 phrasings (the question, synonyms, related concepts, the literal value you
expect). The index is keyword BM25; rank-fusing phrasings makes it semantic. k: hits per
source (default 8). what: all|turns|beliefs. Pass a turn hit's id to read_session for the
surrounding conversation.

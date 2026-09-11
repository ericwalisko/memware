Answers questions like these:
"Why did we choose SQLite over Postgres?"
"What did we decide about the port?"
"Have we tried the async approach before? What went wrong?"
"What happened last session?"
"Which alternative did we reject, and why?"
"Why is this config set the way it is?"
"What was that number/path/version mentioned in chat?"
"Did we already settle this in another repo?"

If the question sounds like one of these, call recall before grepping, guessing, or saying "I don't know". It searches past session transcripts and currently valid beliefs in a keyword (BM25) index; it cannot see the current working tree, so grep for code that exists now.

queries: pass 3-5 phrasings (the question, synonyms, the literal value you expect); fusing them by rank is what makes keyword search semantic. k: hits returned per kind. what: all|turns|beliefs. A turn hit's id goes to read_session for the surrounding conversation.
# Conversational RAG Improvement Guide

A practical guide to evolving a single-turn RAG application into a multi-turn, filter-aware, production-ready system.

---

## Current Architecture

Your current RAG pipeline:

```text
User prompt
   ↓
Extract text (regex → LLM fallback)
   ↓
Query ChromaDB for embedded documents
   ↓
Retrieve top K results
   ↓
LLM synthesizes answer
```

**Limitations:**

- Single prompt only
- No conversation history or session management
- No support for follow-up questions
- No multi-intent handling
- No query rewriting or contextualization
- No reranking or hybrid retrieval

---

## Target Architecture

```text
User message
   ↓
Load conversation state
   ↓
Classify intent and detect topic switch
   ↓
Resolve references / rewrite query
   ↓
Extract structured filters
   ↓
Retrieve documents (dense + lexical)
   ↓
Rerank and build context
   ↓
Generate grounded answer
   ↓
Persist turn, sources, and state
```

---

## 1. Add Sessions and Conversations

### Data Model

Use three separate concepts:

| Concept | Purpose | Example |
|---|---|---|
| User | Owner of the data | `user-123` |
| Conversation/session | A logical chat | `conversation-456` |
| Message/turn | One user or assistant interaction | `message-789` |

### Schema

```sql
CREATE TABLE Conversations (
    Id UNIQUEIDENTIFIER PRIMARY KEY,
    UserId UNIQUEIDENTIFIER NOT NULL,
    Title NVARCHAR(200),
    CreatedAt DATETIME2 NOT NULL,
    UpdatedAt DATETIME2 NOT NULL,
    Summary NVARCHAR(MAX),
    Version INT NOT NULL DEFAULT 1
);

CREATE TABLE Messages (
    Id UNIQUEIDENTIFIER PRIMARY KEY,
    ConversationId UNIQUEIDENTIFIER NOT NULL,
    Role VARCHAR(20) NOT NULL, -- user, assistant, system
    Content NVARCHAR(MAX) NOT NULL,
    CreatedAt DATETIME2 NOT NULL,
    TokenCount INT NULL,
    FOREIGN KEY (ConversationId) REFERENCES Conversations(Id)
);

CREATE TABLE MessageSources (
    MessageId UNIQUEIDENTIFIER NOT NULL,
    DocumentId NVARCHAR(200) NOT NULL,
    ChunkId NVARCHAR(200) NOT NULL,
    RelevanceScore FLOAT NULL,
    PRIMARY KEY (MessageId, DocumentId, ChunkId)
);
```

### What to Persist

- User message
- Assistant answer
- Rewritten standalone query
- Extracted filters
- Retrieved chunk IDs
- Prompt/model metadata
- Latency and token usage
- Retrieval scores
- Detected intent or topic

---

## 2. Memory Strategy

### Short-Term Memory

Keep the latest few turns:

- Last 3–5 user turns
- Their corresponding assistant responses
- A conversation summary for older content

Example state:

```json
{
  "conversationId": "conversation-456",
  "summary": "The user is designing a RAG system using ChromaDB...",
  "recentMessages": [
    {
      "role": "user",
      "content": "How can I add chat history?"
    },
    {
      "role": "assistant",
      "content": "You should add..."
    }
  ],
  "activeTopic": "conversational RAG",
  "entities": {
    "vectorStore": "ChromaDB",
    "retrievalStrategy": "dense search"
  }
}
```

### Long-Term Memory

Store only information that should remain useful across sessions:

- User preferences
- Stable project facts
- Important decisions
- Repeated entities or domain terms

**Do not** automatically embed every message into a memory vector database. Extract candidate memories and save them only when they meet criteria:

- Likely to remain true
- Useful in future conversations
- Explicitly stated or confirmed by the user
- Not sensitive unless the user expects it to be remembered

---

## 3. Add a Contextualization Step

Transform ambiguous follow-ups into standalone queries.

### Input

```text
What about its timeout?
```

### Conversation Context

```text
User: How does the payment service retry failed requests?
Assistant: It retries three times with exponential backoff.
User: What about its timeout?
```

### Output

```json
{
  "standalone_query": "What is the timeout configuration for the payment service retry mechanism?",
  "intent": "document_question",
  "filters": {},
  "requires_retrieval": true,
  "topic": "payment service retry configuration"
}
```

### Responsibilities

The contextualizer should:

- Resolve pronouns and references
- Preserve exact identifiers
- Identify the user's intent
- Extract hard constraints
- Detect whether retrieval is needed
- Split compound questions when appropriate

### Response Contract

```csharp
public sealed record QueryPlan(
    string OriginalQuery,
    string StandaloneQuery,
    QueryIntent Intent,
    IReadOnlyList<string> SubQueries,
    DocumentFilters Filters,
    bool RequiresRetrieval,
    bool NeedsClarification,
    string? ClarificationQuestion
);

public sealed record DocumentFilters(
    string[]? DocumentTypes,
    string[]? ProductNames,
    string[]? Versions,
    string[]? Environments,
    DateTimeOffset? CreatedAfter
);
```

**Key rule:** Keep structured constraints separate from semantic text. For example, "version 3.2", "production", and "only PDFs" should become filters rather than relying on embeddings.

---

## 4. Route Simple and Complex Queries

### Intent Classification

Classify messages into:

```text
- Casual conversation
- Document question
- Follow-up document question
- Multi-intent question
- Clarification request
- Unsupported/action request
```

### Routing Logic

```csharp
switch (queryPlan.Intent)
{
    case QueryIntent.CasualConversation:
        return GenerateWithoutRetrievalAsync(request);

    case QueryIntent.DocumentQuestion:
    case QueryIntent.FollowUpDocumentQuestion:
        return RetrieveAndAnswerAsync(queryPlan);

    case QueryIntent.MultiIntent:
        return RetrieveForSubQueriesAsync(queryPlan.SubQueries);

    case QueryIntent.Clarification:
        return AskClarificationAsync(queryPlan.ClarificationQuestion);
}
```

### When to Use Rules vs LLM

**Use regex/rules for:**

- Dates
- Version numbers
- Document types
- Product IDs
- Error codes
- Environment names
- Exact keywords
- Boolean filters

**Use LLM when:**

- The query contains pronouns
- It refers to previous answers
- It has multiple sentences or intents
- It requires query decomposition
- The user's wording does not match your rules

---

## 5. Improve Retrieval Quality

### Current Pipeline

```text
Extract filters → ChromaDB vector search → top K → LLM
```

### Target Pipeline

```text
Contextualize query
   ↓
Extract filters
   ↓
Dense retrieval + lexical retrieval
   ↓
Merge candidates
   ↓
Rerank
   ↓
Deduplicate
   ↓
Select context
```

### Over-Fetch Before Reranking

Do not retrieve only the final number of chunks you want to send to the LLM.

```text
Retrieve 30–50 candidates
→ rerank them
→ keep the best 5–10 chunks
→ build the generation context
```

### Add Hybrid Retrieval

Dense search handles semantic similarity, while lexical search handles exact terms:

- Class names
- API paths
- Error codes
- Configuration keys
- Product codes
- Version strings

**Implementation options:**

- PostgreSQL full-text search
- Elasticsearch/OpenSearch
- SQLite FTS5
- Lucene.NET
- Another dedicated search engine

### Reciprocal Rank Fusion (RRF)

Merge dense and lexical rankings:

```text
RRFScore(document) =
    1 / (k + denseRank)
  + 1 / (k + lexicalRank)
```

### Add a Reranker

A reranker evaluates the query against each candidate chunk and produces a more precise relevance score.

```text
denseTopK = 30
lexicalTopK = 30
mergedCandidates = RrfMerge(denseTopK, lexicalTopK)
reranked = Rerank(query, mergedCandidates)
finalChunks = SelectDiverseTopN(reranked, 8)
```

**Deduplication:** Avoid returning ten nearly identical chunks from the same document. Apply maximal marginal relevance so the final context covers different parts of the answer.

---

## 6. Handle Multiple Prompts in One Turn

### Query Decomposition

For a query like:

```text
How does authentication work, what are the retry limits, and which configuration file controls them?
```

Decompose into:

```json
{
  "subQueries": [
    "How does authentication work?",
    "What are the retry limits?",
    "Which configuration file controls authentication and retry settings?"
  ]
}
```

### Retrieval and Answer Structure

Retrieve separately for each subquery, merge and deduplicate the results, then preserve the relationship between each subquery and its evidence.

```json
{
  "question": "...",
  "sections": [
    {
      "subQuestion": "How does authentication work?",
      "sources": ["chunk-12", "chunk-18"]
    },
    {
      "subQuestion": "What are the retry limits?",
      "sources": ["chunk-31"]
    }
  ]
}
```

---

## 7. Make the Answer Grounded

### Generation Prompt Structure

```text
System:
You answer questions using the supplied documents.

Rules:
1. Use retrieved documents as the source of truth.
2. Do not invent facts absent from the documents.
3. If the documents are insufficient, say so.
4. Distinguish a document fact from a previous conversation claim.
5. Cite the document or chunk supporting each important claim.
6. Ask for clarification when the rewritten query is ambiguous.

Conversation summary:
{summary}

Recent conversation:
{recentMessages}

Standalone query:
{standaloneQuery}

Retrieved context:
{chunks}
```

**Do not** include the entire history in the generation prompt by default. Use history to contextualize the query, and include only the recent conversation needed to make the response natural.

---

## 8. Detect Topic Changes

### Problem

Conversation history can hurt retrieval when the user changes topics.

```text
User: Explain our payment retry policy.
...
User: How do I configure Kubernetes readiness probes?
```

The second query should not inherit "payment retry policy" merely because it is in the same session.

### Solutions

- Ask the contextualizer to return `topic` and `topicChanged`
- Compare the current query embedding with the active topic embedding
- Reset the short-term context after a strong topic switch
- Keep the conversation but omit unrelated history from query rewriting

### Example

```json
{
  "topic": "Kubernetes readiness probes",
  "topicChanged": true,
  "standaloneQuery": "How do I configure Kubernetes readiness probes?",
  "historyUsed": []
}
```

Only carry forward previous context when the current message contains signals such as "it", "that", "the previous one", "what about", or "how does this work?"

---

## 9. Add an Explicit Clarification Path

### When to Clarify

For ambiguous queries:

```text
What about the other configuration?
```

If there are several configurations in the recent context, return:

```text
Which configuration do you mean: the authentication configuration or the retry configuration?
```

### Response Structure

```json
{
  "needsClarification": true,
  "clarificationQuestion": "Do you mean the authentication configuration or the retry configuration?"
}
```

This is preferable to retrieving arbitrary documents and generating a confident but incorrect answer.

---

## 10. Evaluate Each Pipeline Stage

### Test Dataset

Create a test dataset from real conversations. Include:

- Standalone questions
- Pronoun-based follow-ups
- Topic switches
- Multi-intent questions
- Exact identifier queries
- Queries with metadata filters
- Questions with no relevant documents
- Questions requiring clarification

### Metrics by Stage

| Stage | Metrics |
|---|---|
| Query understanding | Intent accuracy, filter accuracy, rewrite correctness |
| Retrieval | Recall@K, precision@K, MRR, nDCG |
| Reranking | nDCG improvement, relevant chunk position |
| Generation | Faithfulness, answer relevance, citation correctness |
| System | Latency, token usage, cost, failure rate |

### Per-Request Logging

```json
{
  "conversationId": "...",
  "originalQuery": "...",
  "standaloneQuery": "...",
  "filters": {},
  "retrievedChunkIds": ["..."],
  "rerankedChunkIds": ["..."],
  "answer": "...",
  "latencyMs": {
    "contextualization": 220,
    "retrieval": 85,
    "reranking": 140,
    "generation": 900
  }
}
```

**Evaluate query rewriting separately from final answer quality.** A rewrite can look linguistically good but still retrieve the wrong documents.

---

## Implementation Order

Implement improvements in this order:

1. **Add persistence:** `User`, `Conversation`, `Message`, and `MessageSource` tables
2. **Load context:** Last 3–5 turns and a conversation summary
3. **Add contextualizer:** Produces a standalone query and structured filters
4. **Add topic-switch detection**
5. **Support query decomposition** for multi-intent prompts
6. **Over-fetch candidates and add reranking**
7. **Add hybrid lexical plus vector retrieval**
8. **Add grounded citations** and an "insufficient context" response
9. **Add evaluation datasets** and stage-level telemetry
10. **Add semantic long-term memory** only after short-term conversational RAG works reliably

---

## Example Orchestration Service

A clear orchestration service with typed request/response contracts:

```csharp
public async Task<ChatResponse> ChatAsync(ChatRequest request)
{
    var state = await conversationStore.LoadAsync(request.ConversationId);

    var queryPlan = await queryPlanner.PlanAsync(
        request.Message,
        state.Summary,
        state.RecentMessages);

    if (queryPlan.NeedsClarification)
        return ChatResponse.Clarification(queryPlan.ClarificationQuestion!);

    if (!queryPlan.RequiresRetrieval)
        return await answerGenerator.GenerateDirectAsync(request.Message, state);

    var candidates = await retriever.RetrieveAsync(
        queryPlan.StandaloneQuery,
        queryPlan.Filters,
        queryPlan.SubQueries);

    var reranked = await reranker.RerankAsync(
        queryPlan.StandaloneQuery,
        candidates);

    var context = contextBuilder.Build(reranked);

    var response = await answerGenerator.GenerateAsync(
        request.Message,
        queryPlan,
        state,
        context);

    await conversationStore.AppendAsync(
        request.ConversationId,
        request.Message,
        response.Answer,
        queryPlan,
        reranked);

    return response;
}
```

---

## Key Takeaways

1. **Treat each turn as a query plan**, not just a string passed to an embedding model.
2. **Separate concerns:** contextualization, retrieval, reranking, and generation should be distinct stages.
3. **Keep structured constraints separate from semantic text.**
4. **Over-fetch, then rerank** for better retrieval quality.
5. **Evaluate each stage separately** to identify bottlenecks.
6. **Start simple:** a clear orchestration service is sufficient for the first version; you do not need an agent framework immediately.

---

## References

- Query rewriting for multi-turn RAG
- ChromaDB metadata filters and hybrid retrieval
- Conversational RAG with history management
- Reranking and reciprocal rank fusion techniques
text_embedding_models = (
    "OpenAIEmbeddings",
    "HuggingFaceEmbeddings",
    "CohereEmbeddings",
    "ElasticsearchEmbeddings",
    "JinaEmbeddings",
    "LlamaCppEmbeddings",
    "HuggingFaceHubEmbeddings",
    "ModelScopeEmbeddings",
    "TensorflowHubEmbeddings",
    "SagemakerEndpointEmbeddings",
    "HuggingFaceInstructEmbeddings",
    "MosaicMLInstructorEmbeddings",
    "SelfHostedEmbeddings",
    "SelfHostedHuggingFaceEmbeddings",
    "SelfHostedHuggingFaceInstructEmbeddings",
    "FakeEmbeddings",
    "AlephAlphaAsymmetricSemanticEmbedding",
    "AlephAlphaSymmetricSemanticEmbedding",
    "SentenceTransformerEmbeddings",
    "GooglePalmEmbeddings",
    "MiniMaxEmbeddings",
    "VertexAIEmbeddings",
    "BedrockEmbeddings",
    "DeepInfraEmbeddings",
    "DashScopeEmbeddings",
    "EmbaasEmbeddings",
)

vectorstores = (
    "AzureSearch",
    "Redis",
    "ElasticVectorSearch",
    "FAISS",
    "VectorStore",
    "Pinecone",
    "Weaviate",
    "Qdrant",
    "Milvus",
    "Zilliz",
    "SingleStoreDB",
    "Chroma",
    "OpenSearchVectorSearch",
    "AtlasDB",
    "DeepLake",
    "Annoy",
    "MongoDBAtlasVectorSearch",
    "MyScale",
    "SKLearnVectorStore",
    "SupabaseVectorStore",
    "AnalyticDB",
    "Vectara",
    "Tair",
    "LanceDB",
    "DocArrayHnswSearch",
    "DocArrayInMemorySearch",
    "Typesense",
    "Hologres",
    "Clickhouse",
    "Tigris",
    "MatchingEngine",
    "AwaDB",
)

API_KEY = "langchain.request.api_key"
PROVIDER = "langchain.request.provider"
MODEL = "langchain.request.model"
TYPE = "langchain.request.type"
COMPLETION_TOKENS = "langchain.tokens.completion_tokens"
PROMPT_TOKENS = "langchain.tokens.prompt_tokens"
TOTAL_COST = "langchain.tokens.total_cost"

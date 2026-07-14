"""Quick smoke test for the OpenRouter LLM API key and endpoint."""

import os
from openai import OpenAI
import dotenv

dotenv.load_dotenv()
client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url="https://openrouter.ai/api/v1",
)

response = client.chat.completions.create(
    model='z-ai/glm-5.2',
    messages=[{"role": "user", "content": "Reply with the single word: working and then introduce yourself. What is your base model?"}],
    max_tokens=1024,
    temperature=0.0,
    extra_body={"usage": {"include": True}},
)

msg = response.choices[0].message
print("finish_reason:", response.choices[0].finish_reason)
print("content:", msg.content)
print("usage:", response.usage)

"""Quick smoke test for the KIT LLM API key and endpoint."""

import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["KIT_LLM_KEY"],
    base_url="https://ki-toolbox.scc.kit.edu/api",
)

response = client.chat.completions.create(
    model=os.environ["KIT_LLM_MODEL"],
    messages=[{"role": "user", "content": "Reply with the single word: working"}],
    max_tokens=1024,
    temperature=0.0,
)

msg = response.choices[0].message
print("finish_reason:", response.choices[0].finish_reason)
print("content:", msg.content)

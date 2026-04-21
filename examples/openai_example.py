"""Run: pip install openai tokenly && python examples/openai_example.py"""
import tokenly
import openai

tokenly.init()

client = openai.OpenAI()
resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Say hi in 3 words."}],
)
print(resp.choices[0].message.content)
print("→ Run `tokenly stats` to see the cost.")

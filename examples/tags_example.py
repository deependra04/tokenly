"""Tag each call with user/feature metadata so `tokenly stats --by=tag.user` works."""
import tokenly
import openai

tokenly.init(tags={"app": "my-saas"})

client = openai.OpenAI()

for user_id in ["alice", "bob", "carol"]:
    tokenly.configure(tags={"app": "my-saas", "user": user_id})
    client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": f"Greet {user_id}"}],
    )

print("→ tokenly stats --by=tag.user")

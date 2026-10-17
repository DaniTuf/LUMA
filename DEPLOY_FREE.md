# Deploy LUMA free on Hugging Face Spaces

One container (the root `Dockerfile`), HTTPS, a fixed address, works on any network.
Tested locally: page, libraries, sign-in and the Live voice teacher all work in this image.

## 1. Create the Space (5 minutes)
1. Make a free account at https://huggingface.co (no card needed).
2. **New Space** → name `luma` → SDK **Docker** → template **Blank** → hardware **CPU basic (free)** → visibility **Public**
   (a private Space needs every visitor to be logged in to Hugging Face).
3. Your address will be `https://<your-username>-luma.hf.space`.

## 2. Add the keys (Space → Settings → Variables and secrets)
Add these as **Secrets** with the values from your `.env`:
`GEMINI_API_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY` (if used), `GOOGLE_CLIENT_ID`

Add these as **Variables**: `LLM_PROVIDER`, `TTS_PROVIDER`, `VOICE_PROVIDER`, `ALLOWED_EMAIL_DOMAIN`,
`MAX_MESSAGES_PER_USER_PER_DAY`, and `DEV_LOGIN` (`1` = anyone can type any @tuf.edu.pk address, fine for a
short test; `0` = Google sign-in only).

Never upload the `.env` file itself.

## 3. Upload the code
Space → **Files** → **Add file → Upload files**, and drag in from `D:\AI_tutor-main\AI_tutor-main`:
`Dockerfile`, `requirements.txt`, the `backend` folder and the `frontend` folder. Commit.
The Space builds by itself (2-4 minutes) and shows **Running**.

## 4. Google sign-in (only if DEV_LOGIN=0)
Google Cloud Console → your OAuth client → Authorized JavaScript origins → add `https://<your-username>-luma.hf.space`.

## Limits of the free tier
- The Space sleeps after 48 hours without visitors; the first visit wakes it (about a minute).
- Its disk is wiped on every restart or update: accounts and chat history start empty again.
  (Persistent storage is a paid add-on.)
- 2 CPUs / 16 GB RAM: plenty for testing with a few people.

## Updating
Upload the changed files again (Files → Add file). The Space rebuilds by itself.

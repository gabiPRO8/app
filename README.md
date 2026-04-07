# Background Remover MVP

Local web service that removes image background and returns a transparent PNG.

## 1) Local setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

## 2) Quick start script

```powershell
.\run_local.ps1
```

## 3) API usage

Endpoint: `POST /api/remove-bg`
Form field: `file` (image)
Form field: `mode` (`simple` or `advanced`)
Response: PNG with alpha channel

Example with curl:

```bash
curl -X POST "http://127.0.0.1:8000/api/remove-bg" \
  -F "file=@input.jpg" \
  -F "mode=advanced" \
  --output output.png
```

### Processing modes

- `simple`: fast and low-memory, optimized for white backgrounds and logos
- `advanced`: lightweight border-aware segmentation for textured or multi-color backgrounds

Both modes are designed to run inside Render Free constraints.

## 4) Next deployment step

- Create Dockerfile for app
- Deploy to Render, Railway, or Fly.io
- Connect domain and SSL
- Add Cloudflare or CDN caching for static assets
- Add analytics and ads in landing pages

### Render quick guide

1. Push this project to GitHub
2. In Render: New + > Web Service > connect repo
3. Use Docker deployment (Render reads Dockerfile)
4. Deploy and copy generated URL
5. Point your domain DNS to Render

### GitHub push commands (first time)

```powershell
git init
git add .
git commit -m "Initial MVP: remove background service"
git branch -M main
git remote add origin https://github.com/YOUR_USER/YOUR_REPO.git
git push -u origin main
```

### Deploy from Render (fast path)

1. Create a new Web Service from your GitHub repo
2. Render will detect `render.yaml`
3. Confirm env vars:
  - `MAX_FILE_MB=10`
  - `RATE_LIMIT_PER_MINUTE=20`
  - `WHITE_THRESHOLD=245`
  - `WHITE_SOFTNESS=20`
  - `ADVANCED_TOLERANCE=46`
  - `ADVANCED_SOFTNESS=24`
  - `ADVANCED_BG_CLUSTERS=8`
4. Deploy and test:

```bash
curl -X GET "https://YOUR_RENDER_DOMAIN/health"
```

### Render production checklist

1. Add environment variables from `.env.example`
2. Verify health endpoint: `/health`
3. Test upload with a real image after deploy
4. Set custom domain in Render dashboard
5. Add DNS records at your domain provider:
  - CNAME `www` -> your Render hostname
  - Optional redirect from root domain to `www`
6. Wait for SSL certificate to be issued automatically

### Domain buying options

- Cloudflare Registrar (low margin pricing)
- Namecheap (simple UI)
- Porkbun (often cheap first year)

Tip: buy short, easy-to-pronounce names and avoid hyphens.

### Domain connection quick steps

1. In Render: Settings > Custom Domains > add `www.yourdomain.com`
2. In your registrar DNS, create CNAME `www` to Render target host
3. Optional: redirect root (`yourdomain.com`) to `www.yourdomain.com`
4. Wait for SSL to become active, then retest `/health`

## 5) Ads and monetization

- Add Google AdSense in your landing page only
- Keep the processing endpoint ad-free and fast
- Add a premium tier later: no ads, HD output, and batch processing

### AdSense practical flow

1. Publish legal pages: privacy policy and terms
2. Add basic content pages (how it works, FAQ, contact)
3. Apply to AdSense once site has enough useful content
4. Place ads in landing and blog pages, not inside upload flow
5. Track Core Web Vitals to keep UX and SEO healthy

### Before applying to AdSense

1. Confirm legal pages are live:
  - `/privacy`
  - `/terms`
2. Add a contact email in privacy page
3. Add at least 5-10 useful pages/posts besides the tool page

## 6) Notes

- Max upload size is 10 MB
- This MVP processes one image per request
- Rate limit is enabled per IP (`RATE_LIMIT_PER_MINUTE`)
- For production at scale, replace in-memory rate limit with Redis
- Add auth, logging, and automatic file cleanup before heavy traffic

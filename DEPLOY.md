# Redeploying HallucinationNerd (after the one-time AWS setup)

The AWS setup (EC2, S3, CloudFront, ALB, ACM, Cloudflare) is **one-time** and
never repeated. Day-to-day updates are just the steps below.

Live site: https://hallucinationnerd.org
API:       https://api.hallucinationnerd.org

Always start by pushing your change to GitHub `main`:
```
git add -A && git commit -m "..." && git push origin main
```

---

## Backend changes (anything in `web/` — Python)
SSH into the server and run the redeploy script:
```
ssh -i hallucinationnerd.pem ubuntu@<EC2-PUBLIC-DNS>
~/HallucinationNerd/web/redeploy.sh
```
It pulls the latest code, installs any new dependencies, restarts the
service, and prints `OK: backend is up (HTTP 200)` when done.

(Under the hood the app runs as the systemd service `hallucinationnerd`.
Logs: `sudo journalctl -u hallucinationnerd -n 50`.)

---

## Frontend changes (anything in `frontend/` — index.html, app.js, env.js)
CloudFront caches the files, so two steps:
1. **S3** → bucket `hallucinationnerd.org` → upload the changed file(s) (overwrite).
2. **CloudFront** → the distribution → **Invalidations** → **Create invalidation**
   → path `/*` (clears the cache; takes a couple of minutes).

---

## What you never touch again
EC2 launch, nginx, systemd, security groups, the S3 bucket creation,
the CloudFront distribution, the ALB / target group, the ACM certificates,
and the Cloudflare DNS records. Those stay as-is unless the domain or the
architecture changes.

# Space Zone deployment

The app is configured for Render. It uses a persistent disk for SQLite data and
environment variables for secrets. The local database is not copied to Render.
User accounts and saved tool results are stored in the SQLite database. Passwords
are stored as hashes; dashboard pages and downloads are marked not to be cached.

## Local development

Set `FLASK_DEBUG=true` before starting `app.py` to enable the development
server's code reloader and browser refresh when files in `templates/` or
`static/` change. Do not enable debug mode in production.

To use local admin email recovery, configure `ADMIN_RECOVERY_EMAIL`,
`RESEND_API_KEY`, and `SPACEZONE_FROM_EMAIL`, and set
`SPACEZONE_PUBLIC_URL=http://127.0.0.1:5000` before starting the app. The reset
link opens on the same computer where the local server is running.

## Before you deploy

1. Create a private GitHub repository and publish this project with GitHub
   Desktop. If Git is not installed, install GitHub Desktop first. Include
   `app.py`, `templates/`, `requirements.txt`, `render.yaml`, and `.gitignore`.
   The ignore rules exclude `.venv/`, `.env` files, and `instance/*.db`; verify
   these are not included before publishing.
2. In Render, create a Blueprint from the repository and apply the `render.yaml`
   configuration. The Blueprint creates a paid Starter web service and a 1 GB
   persistent disk; check Render's current pricing before creating resources.
3. In the service's Environment settings, set:
   - `ADMIN_USERNAME`: a new administrator username.
   - `ADMIN_PASSWORD`: a unique password of at least 12 characters.
   - `ADMIN_RECOVERY_EMAIL`: the private email address authorized to reset the
     admin password.
   - `SPACEZONE_PUBLIC_URL`: the public HTTPS origin of the deployed site,
     such as `https://your-domain.example` (no path).
   - `RESEND_API_KEY` and `SPACEZONE_FROM_EMAIL`: required for password reset
     and welcome emails. The sender must be verified with Resend.
4. Confirm the automatically generated `FLASK_SECRET_KEY` is present and keep
   all secret values private. Do not put secrets in source files or commit them.
5. Deploy and check the service health at `/healthz`. Test the homepage, chat,
   user sign-up/sign-in and saved-result dashboard, admin sign-in and password
   recovery, email form, and the required browser tools. Do not enable Flask
   debug mode in production.

Admin password recovery sends a one-time link to `ADMIN_RECOVERY_EMAIL`. Links
expire after 30 minutes. The reset password is stored in the persistent app
database; after the first email reset, it takes precedence over
`ADMIN_PASSWORD` while `ADMIN_USERNAME` remains the admin username. Keep the
database disk persistent and keep recovery-email settings enabled. Before the
first email reset, if recovery is not configured, update `ADMIN_PASSWORD` in
the service environment instead.

The Starter and Growth prices are ₹399/month and ₹999/month. The pricing page
contains UPI one-time payment intents; renewals, payment verification, and
paid-account activation are manual and are not implemented. Do not describe
these as automatic subscriptions or promise access based on an unverified payment.

## Connect `spacezone.com`

After the first successful Render deploy:

1. Open the Render web service settings and add `spacezone.com` and
   `www.spacezone.com` as custom domains.
2. At the domain registrar, add the DNS records Render displays for both
   hostnames. Remove conflicting `AAAA` records if Render's instructions
   require it. Do not guess the target record values.
3. Wait for Render to verify the DNS and provision TLS, then test both HTTPS
   hostnames. Choose one canonical hostname and configure a redirect if needed.

DNS changes and deployment require access to the Render and domain registrar
accounts. This project setup does not publish the code or change DNS records.
See Render's [Blueprint](https://render.com/docs/blueprint-spec),
[persistent disk](https://render.com/docs/disks), and
[custom domain](https://render.com/docs/custom-domains) documentation.

## Data and email

SQLite is stored at `/var/data/spacezone.db` on the Render persistent disk.
Back up that disk before migrations or destructive changes. The existing local
SQLite database is intentionally excluded from deployment and is not migrated.

Email delivery is optional. Without the Resend variables, requests can be
recorded but welcome messages are not sent.

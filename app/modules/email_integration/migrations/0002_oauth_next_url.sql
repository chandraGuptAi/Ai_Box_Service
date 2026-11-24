-- The page to show in the sign-in tab once the draft is ready (Gmail / Outlook draft link).
-- After Google/Microsoft sign-in the browser may cut the AI Box page's link to that tab, so the tab itself asks
-- GET /api/email/oauth/next?state=... for this URL and opens it.
ALTER TABLE email_oauth_transactions ADD COLUMN next_url text;

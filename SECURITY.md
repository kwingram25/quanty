# Security

quanty holds exchange API keys and places real orders, so a security problem in it can cost
someone money. Please report one privately, not in a public issue.

## Reporting

Use GitHub's private reporting: the repository's **Security** tab → **Report a vulnerability**. Say
what's affected, how to reproduce it, and what it lets someone do. You'll get a reply within a
week; a fix goes out as a patch release, and the advisory credits you unless you'd rather not.

Especially welcome:

- anything that could place, change or cancel an order without the live broker being armed, or
  past its caps (`max_qty`, `max_notional`)
- keys leaving where they belong: into logs, the dashboard, recordings, the Docker image, or what
  `quanty deploy` sends
- the dashboard: getting past its login, or a page that lets another site act on a running session
- `quanty deploy`, `keys`, `server` or `mcp` doing more on a server than they say

## Versions

Fixes go into the latest release. While quanty is at 0.x, that's the newest 0.x version; upgrade
with `quanty upgrade` in your project.

## Not quanty's to fix

Losses from a strategy doing what it was written to do, a venue's own outage or API change, or a
server you run without the firewall and password the docs set up. Those are risks of using it (see
the README's "Use at your own risk"), not vulnerabilities.

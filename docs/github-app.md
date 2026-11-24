# The GitHub App

> This runtime no longer uses a GitHub App: Doppelwerk holds the App (Doppelcloud) and its key, posts the reviews and approves; a runner fetches with each job's one-repository read token. Kept for the record; see [specs/bff-split.md](specs/bff-split.md).

How to create the App the runtime posts as, connect it, and check it works. For whoever has admin rights on the org, and for whoever runs the runtime.

The App is the reviewers' identity on GitHub. Reviews, comments and the merger's approval appear as `<app-name>[bot]`. A personal reviewer's comments are labelled "steven's doppel" but come from the App, never from Steven's own account, so they never mix with his own reviews.

## 1. Create it

**Who:** an owner of the org, or a member they've given the "GitHub App manager" role (org **Settings → Developer settings → GitHub Apps → Management**).

**Where:** https://github.com/organizations/summationai/settings/apps/new (org **Settings → Developer settings → GitHub Apps → New GitHub App**).

| Field | Value |
| --- | --- |
| Name | `doppelwerk-dev`. Names are unique across GitHub, and `doppelwerk` is RC's production App. |
| Homepage URL | Any Summation page. |
| Webhook | Untick **Active** for now: the runtime polls. To turn webhooks on later, see section 4. |
| Repository permissions | **Pull requests:** read and write. **Contents:** read. **Checks:** read. **Commit statuses:** read. **Metadata:** read (always on). Nothing else. Never contents write. |
| Where can this App be installed | **Only on this account.** See section 5 before changing it. |

After creating it:

1. Note the **App ID** shown at the top of the App's page.
2. Under **Private keys**, click **Generate a private key**. A `.pem` file downloads. It is the App's only secret: don't commit it, paste it into chat, or email it.
3. **Install App → summationai → Only select repositories →** pick the sandbox repo. The URL you land on ends in `/installations/<number>`. That number is the **installation ID**.

## 2. Connect the runtime

On the machine running the runtime, as the runtime's user:

```sh
install -m 600 ~/Downloads/doppelwerk-dev.*.private-key.pem /var/lib/doppel/github/app.pem
```

The runtime refuses a key file that anyone but its owner can read. Then, in the config file:

```toml
[github]
mode = "app"
app_id = "123456"
private_key = "/var/lib/doppel/github/app.pem"

[[tenants]]
id = "summation"
repos = ["summationai/doppel-sandbox"]
installation_id = 7890123
```

## 3. Check it

```sh
uv run python scripts/live_github_check.py --config doppel.toml
```

This is read-only. It checks three things and prints no token:

1. The key buys a token for each installation.
2. The poller's reads work on each repo.
3. A mirror fetches an open PR with the token and doesn't store it.

Then run `uv run doppel-runtime serve --config doppel.toml` and open a PR on the sandbox repo. That's checkpoint 6b's live run.

## 4. Webhooks (optional)

Polling alone works. Webhooks make the runtime react in seconds instead of within a poll interval.

GitHub must reach a public URL, and the runtime is on the tailnet, so the BFF receives the webhook and forwards the raw body and headers untouched to the runtime's internal listener at `POST /github/webhook`. The runtime holds the webhook secret and checks GitHub's signature itself (RC's review #7). A webhook only makes the runtime poll that repo at once, so a lost one costs at most a poll interval.

1. Generate a secret: `openssl rand -hex 32 > /var/lib/doppel/github/webhook-secret && chmod 600` that file.
2. On the App's page, tick **Active**, set the URL to the BFF's forwarding endpoint, and paste the same secret.
3. Subscribe to these events: pull request, pull request review, pull request review thread, pull request review comment, issue comment, check run, check suite, status.
4. Add `webhook_secret = "/var/lib/doppel/github/webhook-secret"` under `[github]`.

## 5. Before making it installable by anyone

Setting "Any account" lets anyone install the App from its link. Before that, these must hold:

- **An install alone runs nothing.** The runtime reviews only repos listed under an onboarded tenant with a payer. This holds today, because tenants come from the config file. It must stay true when the BFF provides them.
- **Linking an install to a tenant is checked.** The BFF's setup page receives an `installation_id` anyone could forge, so it must confirm, through the user's GitHub sign-in, that the user can access that install. (Checkpoint 8.)
- **Strangers' PRs run nothing unasked.** Automatic reviews run only on PRs from owners, members and collaborators (`[intake] auto_review = "insiders"`, the default). An outsider's PR runs a reviewer only when one is requested, or when an insider's `@doppelwerk` command asks.
- **Running strangers' code**, even read-only, is a question for summini (checkpoint 7) and gVisor.
- **Spending a user's ChatGPT plan on other people's PRs** is the open vendor question in the spec, section 12.

A private App can be made public later, from its settings. Once other accounts have installed it, it can't be made private again.

# LinkedIn API MCP

<p align="left">
  <a href="https://github.com/tmedford/linkedin-api-mcp/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/tmedford/linkedin-api-mcp/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/tmedford/linkedin-api-mcp/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-%233fb950?labelColor=32383f" alt="License"></a>
</p>

An MCP server that gives Claude and other AI agents LinkedIn data as structured records, read from the same API LinkedIn's own web client calls, through your own logged-in browser session. Profiles, companies, jobs, posts, your inbox, your invitations and who viewed your profile, plus sending messages and connection requests.

It is a fork of [stickerdaniel/linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server), which reads LinkedIn by scraping the pages it renders. This fork keeps that project's browser and session handling and replaces every data tool with one that asks the API instead.

> This is an independent open-source project, not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn or Microsoft. LinkedIn is a trademark of LinkedIn Corporation and is used here only to identify the service this software interacts with.

## Why read the API instead of the page

A scraper returns what LinkedIn chose to paint on one screen. That is a sample of the data, and reading it has side effects. The cases below are the ones that led to this fork.

| Question | Scraping the page | Reading the API |
|----------|-------------------|-----------------|
| "Have I replied to everyone?" | Sees roughly the first 16 conversations in the sidebar, and finds each thread's id by clicking it, which marks it read | Any page of the mailbox in one request. Nothing is clicked, so nothing is marked read |
| "What did they say?" | Opens the thread, which marks it read, and returns one block of text with the page's menus in it | Each message as a record with sender and time. The thread stays unread |
| "Find people who match this" | The results page as text, 10 at a time | Up to 50 people a page, each a record with an identifier the next tool accepts |
| "Tell me about this person" | One page load per profile section, each returned as text | The whole profile in one request, with contact info and what you share with them |
| "Message this person" | Opens their profile, hunts for a Message button and types into a composer. Fails when the button is ambiguous or missing | Resolves the member and posts to the messaging API |
| "When was this posted?" | "3w", as displayed | The actual timestamp, with engagement counts |

Three more differences apply to every tool:

- **Records, not text.** Results come back as fields an agent can filter and compare, so it does not have to parse a page dump.
- **Outputs chain into inputs.** Every person carries a `public_identifier` and every company a numeric `company_id`, in the form the next tool takes. Searching for people at a company and then reading each profile needs no conversion step.
- **An empty result means empty.** A response that fails to parse raises an error. It is never reported as zero results, because "no unread messages" and "the reader broke" must not look the same.

## Tools

Every tool reads or writes through the API except two that upstream provides unchanged: `get_job_apply_url`, which reads the posting's page, and `close_session`.

**Messaging**

| Tool | What it does |
|------|--------------|
| `get_conversations` | One page (25) of your conversations, cursor-paged across the whole mailbox, with participants, last activity, read state and whether a reply is owed |
| `get_conversation` | The recent messages of one thread, by thread id or by person, without marking it read |
| `search_conversations` | Conversations matching a keyword |
| `send_message` | Send a message to a person. Requires `confirm_send`; without it the call is a dry run |
| `reply_to_thread` | Reply inside an existing thread, including InMail and Open Profile threads. Requires `confirm_send` |

**People**

| Tool | What it does |
|------|--------------|
| `get_person_profile` | A person's whole profile: experience, education, skills, certifications, projects, contact info and what you have in common |
| `get_my_profile` | Your own profile, in the same shape |
| `get_person_posts` | A person's posts and reposts, with timestamps, engagement counts and the original of anything reshared |
| `get_mutual_connections` | The connections you share with a person, with LinkedIn's total. These are the people who could introduce you |
| `search_people` | Search by keywords, location, connection degree and current company |
| `get_sidebar_profiles` | The profiles LinkedIn suggests beside a person's profile |

**Your network**

| Tool | What it does |
|------|--------------|
| `get_invitations` | One page of your invitation manager |
| `connect_with_person` | Send a connection request or accept an incoming one, with an optional note. Supports `dry_run` |
| `get_profile_views` | Everyone LinkedIn lists as having viewed your profile, over 7 to 365 days, filterable by company, industry or location |
| `get_recruiter_views` | Which recruiters viewed your profile, by company, with a link to that company's open roles |

**Companies**

| Tool | What it does |
|------|--------------|
| `get_company_profile` | A company's profile, including the numeric `company_id` other tools take |
| `get_company_posts` | One page of a company's posts |
| `get_company_employees` | People at a company and its demographics, filterable by keyword and school |
| `search_companies` | Search for companies by keyword |

**Jobs and content**

| Tool | What it does |
|------|--------------|
| `search_jobs` | Search jobs by keywords and location |
| `get_job_details` | One job posting in full |
| `get_saved_jobs` | The jobs in your tracker, by stage |
| `get_job_apply_url` | How a posting takes applications, and the employer's application link. From upstream; reads the page without clicking anything |
| `get_feed` | Posts from your home feed |
| `search_posts` | Search posts by keyword. Unlike searching on the page, nothing is added to your search history |

**Session**

| Tool | What it does |
|------|--------------|
| `close_session` | Close the browser session and clean up |

## Install

**Prerequisite:** [uv](https://docs.astral.sh/uv/getting-started/installation/).

This fork is installed from GitHub. The PyPI package `mcp-server-linkedin` is the upstream project and does not contain these tools.

**Claude Code**

```bash
claude mcp add linkedin -- uvx --from git+https://github.com/tmedford/linkedin-api-mcp mcp-server-linkedin
```

**Claude Desktop and other MCP clients**

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/tmedford/linkedin-api-mcp",
        "mcp-server-linkedin"
      ],
      "env": { "UV_HTTP_TIMEOUT": "300" }
    }
  }
}
```

**Signing in.** The first tool call that needs a session opens a browser window for you to log in to LinkedIn, or imports the session from a Chromium browser you are already signed in to. To do it ahead of time:

```bash
uvx --from git+https://github.com/tmedford/linkedin-api-mcp mcp-server-linkedin --login
```

**Updating.** uvx caches the install. To pick up new commits, add `--refresh` after `uvx` once.

**From source**

```bash
git clone https://github.com/tmedford/linkedin-api-mcp
cd linkedin-api-mcp
uv sync
uv run -m linkedin_mcp_server --no-headless
```

Command-line options, HTTP transport, Docker, proxies and troubleshooting are unchanged from upstream and documented in upstream's README, which this repository keeps unmodified at [`README.md`](../README.md). Where it says `uvx mcp-server-linkedin@latest`, use the `uvx --from git+...` form above. Upstream's published Docker image and Claude Desktop bundle contain upstream's tools; to run this fork in Docker, build the image from this repository with `docker build -t linkedin-api-mcp .`.

## How it works

The server runs a real Chromium browser on your machine, logged in as you. Each API request is issued from inside that logged-in LinkedIn page, the same way the page's own JavaScript issues it. Your session stays in the browser profile on your disk. No password or cookie is sent anywhere except to LinkedIn, and there is no hosted service in between.

The fork's code lives in one package, [`linkedin_mcp_server/voyager/`](../linkedin_mcp_server/voyager/). At startup it removes each upstream tool it replaces and registers its own under the same name and arguments, so a client configured for upstream keeps working. Upstream's files are never edited, only added to, which is enforced by [a test](../tests/test_fork_divergence_is_additive.py) so that upstream's work on the browser and session layer can keep being merged in.

## Is this safe to use?

Be clear about what this is before you use it:

- The API these tools call is the internal one LinkedIn's web client uses, often called Voyager. It is undocumented, LinkedIn can change it without notice, and a tool can break when it does.
- LinkedIn's [User Agreement](https://www.linkedin.com/legal/user-agreement) prohibits automated access. Accounts that use automation can be restricted or banned. There is no guarantee of account safety, and you use this at your own risk.
- It is for personal use on your own account, with no warranty of any kind.
- Tool calls run one at a time through a queue, and the tools that write (messages, replies, connection requests) need an explicit confirmation flag. You are still responsible for how much automation you run. Use it sparingly.

## Contributing

Bug reports and requests go in [Issues](https://github.com/tmedford/linkedin-api-mcp/issues). When a tool returns something wrong or stops working, include the tool name, the arguments and the error text.

Problems with login, the browser, sessions or Docker usually belong to the shared layer, so check whether they also happen in [upstream](https://github.com/stickerdaniel/linkedin-mcp-server/issues) first.

## Acknowledgements

This project exists because of [Daniel Sticker's linkedin-mcp-server](https://github.com/stickerdaniel/linkedin-mcp-server), which built and maintains the browser, session and packaging layer everything here runs on. If that work is useful to you, star and support the original.

Built with [FastMCP](https://gofastmcp.com/) and [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).

## License

Apache 2.0. See [`LICENSE`](../LICENSE) for terms and [`NOTICE`](../NOTICE) for attribution.

# EDGAR Research Agent

Answers questions about companies that file with the U.S. Securities and Exchange Commission (SEC), from their own filings. Every figure in an answer is looked up in a filing and checked against it before the answer is given, and each answer shows where its figures came from.

A checked answer is not a guaranteed answer. The plugin makes sure each figure is the one in the filing; it cannot make sure the question was read the way you meant it. Every answer says so in its first line, and shows its working so that you can check it.

> **Where it runs today:** Claude Code and Cowork on **macOS**. Windows is not supported yet, and neither is chat on claude.ai. Needs Python 3.9 or later.

Ask in plain words:

- "What was FedEx's revenue in its last fiscal year?"
- "How much did it grow from the year before?"
- "Compare Starbucks' operating margin with its margin three years ago."
- "At 25 times next year's earnings guidance, what share price is implied?"

## What you get in each answer

- **A first line that says what was done and what was not:** that every figure was traced to an SEC filing and checked against it, and that this is not a guarantee.
- **The answer, marked as the answer.**
- **The sources**, each filing as a link that opens it on sec.gov.
- **The working:** every figure with its filing, and every calculation step.
- **A saved copy** of the full answer in `Documents/EDGAR Research` on your computer.
- **Two links, "Was this answer right? Yes · No"**, so you can tell the publisher in one click. Clicking is optional.

Ask for "just the number" and you get a short answer; the figure is still looked up and checked. Ask "show me the full working" and you get the rest.

## Where it works

| Claude app | Status |
|---|---|
| Claude Code on macOS | Yes. This is where the plugin was developed and measured. |
| Cowork on macOS, when the task runs on your computer | Yes. Tried on 26 Nov 2026 on a second Mac that had never had the plugin: installed from the package and asked ten questions, all of which behaved as expected. The trial used the publisher's own Claude account. |
| Chat on claude.ai | No. Chat does not run the checking program this plugin depends on. |

| Your computer | Status |
|---|---|
| macOS 13 or later | Supported |
| Linux | Not tested |
| Windows | Not supported yet |

What you need:

- **A paid Claude plan.** In our trial, an account on the Free plan could not use Cowork.
- **A current version of the Claude desktop app** (for Cowork). An older copy asked to be updated first.
- **Python 3.9 or later**, reachable as `python3`. The plugin does not install Python for you. To check, open Terminal and type `python3 --version`. If your Mac does not have it, macOS may offer to install Apple's "command line developer tools", which include Python: accept, wait for it to finish, and ask your question again. You can also start that install yourself by typing `xcode-select --install` in Terminal, or download Python from https://www.python.org/downloads/. Nothing else has to be installed.

**Permission prompts.** The first time the plugin is used, Claude asks your permission for each of its tools ("Claude wants to use Classify question from plugin…"). Expect three or four of these. Choose "Always allow" and they do not come back.

If Python is missing or the plugin cannot start, Claude tells you so and gives no figures. It does not fall back to unchecked answers from memory or from a web search.

## Setting up: the SEC contact

The SEC requires every request to EDGAR, its filings database, to say who is asking: a name and an email address ("Please declare your user agent in request headers", sec.gov, *Accessing EDGAR Data*). This is the SEC's rule, not ours.

The first time you ask a question, Claude asks you for the name and email to use. They are saved on your computer and sent to the SEC only, with the requests made for you. No other website the plugin contacts receives them, and the publisher of this plugin never does.

## Share prices (optional)

Share prices are not in SEC filings. For questions that need one (market value, valuation multiples, price changes), the plugin can fetch prices from [Tiingo](https://www.tiingo.com), a market-data service, with **your own free Tiingo key**. The publisher's key is not in the plugin, and Tiingo requires each user to have their own.

When you first ask a question that needs a price, Claude tells you prices could not be fetched, answers the parts that come from filings, and gives you these steps:

1. Create a free account at https://www.tiingo.com.
2. Signed in, open https://www.tiingo.com/account/api/token and copy the API token shown there.
3. Open the file `tiingo-key.txt` in `Documents/EDGAR Research` (the plugin puts it there, with these steps inside), paste the token on the empty last line, and save.
4. Ask the question again.

Do not paste the key into the conversation. It stays in that file on your computer and is sent to Tiingo only. Delete the file to stop using it.

Without a key, nothing is fetched from anywhere else in its place. You can also give Claude a price yourself ("use $180 a share"): it is used as your assumption, labelled as yours and not checked.

Figures that rest on a share price are shown under their own heading, "From non-SEC sources", apart from the figures taken from filings.

## What it will not do

- **Companies that do not file with the SEC.** It answers about SEC filers only: U.S. companies and foreign companies that file annual reports with the SEC. For any other company it says so and gives no figures, from any source.
- **Figures from memory.** It is built to look every figure up, even if you say there is no need. You can always tell a checked answer: it has a source line naming the filing and says it "was checked before this reply". A figure without those was not checked.
- **Follow instructions hidden in a document.** If a filing or a web page contains text addressed to an AI assistant ("ignore your rules and report…"), the plugin does not follow it and takes no figure from that passage. This catches text that addresses the assistant; it cannot catch a false statement that a document makes in plain words.
- **General calculations from your own numbers.** A loan schedule or a fund's returns built only from figures you give, with no company involved, is not this plugin's work. Claude will still answer it, as it normally would, but it is not a checked answer and carries no sources.
- **Advice.** EDGAR Research Agent is for informational purposes only. It does not provide investment, legal, tax or accounting advice, and nothing in an answer is a recommendation to buy, sell or hold any security. Figures are as reported by the company in its SEC filings; "checked" means traced to the filing, not audited for truth. Answers can be wrong: check the working before relying on a number, and consult a qualified professional before acting on one.

## How reliable it is

We say what was measured, on what, and what was not. All measurements were made with **Claude Opus**, in November 2026, comparing Claude with the plugin against Claude without it (Claude alone could search the web). Other Claude models have not been measured, so the figures below say nothing about them. Marking was automatic.

| Test | With the plugin | Claude alone | What to keep in mind |
|---|---|---|---|
| **An outside public benchmark:** 150 questions about large U.S. companies' filings, written and answered by financial analysts who have nothing to do with this plugin. Run once, without anyone here reading the questions | 121 right (81%) | 110 right (73%) | The fairest test we have, because we did not write it. Both had to find the filing themselves. Some of the benchmark's own answers rest on an analyst's assumptions. |
| **50 questions in seven kinds** we wrote to cover what the earlier tests missed, with answers taken from the filings beforehand | 46 right (92%) | 29 right (58%) | Written by the same assistant that built the plugin. By kind, with the plugin: foreign companies 7 of 7; industry measures of banks, insurers and others 8 of 8; fiscal calendars and share splits 7 of 7; companies renamed, merged or no longer reporting 8 of 8; proxy statements, insider forms, 13F and other forms 7 of 7; questions answered in words 6 of 7; **several companies at once or long series of years 3 of 6**. |
| **67 hard multi-step questions** written by the plugin's builder and set aside: the plugin was never adjusted to their individual results. Measured 24 Nov on the released code | 62 right (93%) | 30 right (45%) | Same author as the plugin, so expect a stranger's questions to land nearer the first row than this one. |
| **The same question asked three times:** 15 questions | The key figures were the same in all three runs for all 15 | not run | Questions the plugin had got right before, so this shows it is steady where it is right. |

**Where it is weak.** Questions that compare several companies, or follow one company over many years, are the plugin's weakest kind: 3 right of 6 in our test (Claude alone: 2 of 6). On an earlier public benchmark of 47 analyst questions that we had studied while building, the headline figure of 15 of 46 answers differed from the benchmark's answer; we judged 6 of those to be plain errors and 9 to rest on a different but defensible basis or a doubtful benchmark answer. That judgment is ours and has not been reviewed by anyone else. The errors were mostly multi-step: a different basis (reported or adjusted earnings, which version of guidance), a figure that sits only in a slide image, or a long calculation. Answers that take several steps say so; check the working of any answer you intend to rely on.

**What it costs.** Checking every figure takes more work: in our tests the plugin used about four to six and a half times as much plan usage per question as Claude alone, and it is slower. For a simple one-figure lookup, Claude alone is usually right too; what the plugin adds there is the filing the figure came from and the check against it.

**Not tested.** Claude models other than Opus. Linux. A Claude account other than the publisher's. A Mac that does not already have Python. Conversations of many messages (tried by hand only). The watchlist, PDF reading and saved conventions (automated tests of the code only). A review in which a person opens the cited filing for a random sample of answers has been done only in part.

## What it stores and sends

| What | Where it goes |
|---|---|
| Your SEC contact (name and email) | Saved on your computer (the folder `.edgar-agent` in your home folder); sent to the SEC with each request to it, and to nobody else |
| Your questions | Not sent anywhere by this plugin. Company names and filing searches go to the SEC's public website, as they would from a browser |
| Your Tiingo key, if you add one | Kept in `Documents/EDGAR Research/tiingo-key.txt` on your computer; sent to Tiingo only, with each price request |
| Share-price requests, if you added a key | The ticker and the dates go to Tiingo |
| Web pages, when a figure for an SEC filer is only on the company's own website | Fetched from that website. The website sees the request, as it would from a browser; it does not receive your SEC contact |
| Answers and their working | Saved on your computer: a copy for you in `Documents/EDGAR Research`, and the plugin's own records and a cache of what it fetched in `.edgar-agent` in your home folder |
| A rating, only if you click "Yes" or "No" under an answer | Sent to a web form of the publisher (a Google Form). "Yes" sends your rating, the kind of question and the plugin's version. "No" opens the form with your question filled in, so the publisher can look into it; you can edit or delete it before pressing Submit. The answer itself, and anything about you, is never included |
| Anything else to the publisher of this plugin | Nothing. If you choose to register for updates, you do that yourself in a web form |

The plugin has no settings to fill in when you install it.

## Register for updates (optional)

If you would like to hear about new versions, you can register with the publisher using [this form](https://docs.google.com/forms/d/e/1FAIpQLScTjUgKCIHGL9XEoQgKbPqA9q4upMmEAkpabwPTZVjahlxVhA/viewform). It asks for an email address and carries its own privacy notice. Registering is separate from using the plugin: the plugin mentions the form once, after your first answer, and sends nothing to the publisher itself.

## How it works, briefly

The plugin adds a small program that runs on your computer and gives Claude tools for EDGAR: finding a company, reading its filings and their reported data, recording each figure with its source, and calculating. Claude must finish through a checking step, which refuses an answer whose figures do not trace to a recorded source. A second check at the end of each reply is meant to stop any figure that was not in a checked answer.

## If something goes wrong

- **"python3: command not found", a macOS window offering to install "command line developer tools", or the `edgar` server shows as failed** (`/mcp` in Claude Code): your Mac has no Python yet. Accept the install that macOS offers, or type `xcode-select --install` in Terminal, or download Python 3.9 or later from https://www.python.org/downloads/. When it has finished, ask your question again.
- **Cowork is not offered, or the app asks to update:** Cowork needs a paid Claude plan and a current desktop app.
- **Claude says the plugin cannot run and gives no figures:** that is the plugin working as intended when its program did not start. The message says what it found.
- **Claude keeps asking for the SEC contact:** give a real name and email; placeholder addresses are refused.
- **"Share prices could not be fetched":** follow the four steps above. If you already added a key, the message says whether Tiingo refused it or its request limit was reached.
- **"does not file with the SEC" for a company that does:** ask again using the company's registered name or its ticker symbol.
- **A figure looks wrong:** say so in the conversation. The plugin records your correction with the answer on your computer.

## For developers

Load the plugin for one Claude Code session without installing it:

```
claude --plugin-dir ./edgar-research-agent
```

## License

Free to use, including at work. You may not use it to provide a product that competes with this one. The terms are the PolyForm Shield License 1.0.0: see [LICENSE](LICENSE) or https://polyformproject.org/licenses/shield/1.0.0.

Copyright 2026 Fares Bagh.

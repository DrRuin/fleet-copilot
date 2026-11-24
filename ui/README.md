# ui

A plain HTML, CSS and JavaScript page with no build step. `app/main.py` serves it at `/`.

| File | Job |
|---|---|
| `index.html` | Sign-in dialog (company and name), sidebar with example questions, thread, side panel |
| `app.js` | Reads the server sent events, renders answers, citations, approval cards and the audit log |
| `style.css` | Light theme |

What a person sees:

- **The answer** once it is checked, each sentence with its refs, and a line saying how many factual sentences were checked or withheld.
- **How it was answered**, the plan, each tool call with its arguments, and the refs it returned.
- **A ref** opens its row and the snapshot ids behind it. A snapshot id opens the raw JSON.
- **An approval card** for each proposal that passed its rule, with the evidence code found, a note field, **Approve** and **Reject**. A refused proposal shows the rule that refused it.
- **Audit log and actions**, every event for the company, newest first, and the actions carried out with their approver.

# Employee workflow improvements

Measured in a real browser (Microsoft Edge driven by Playwright) on a separate sample database
(`bankdb_ux`) filled with synthetic customers through the application's own screens. Each task
starts on the dashboard. **Clicks** include confirming dialogs and pressing Enter to search;
**fields** are values typed or chosen. Counts come from the walkthrough scripts' output; nothing
below is estimated.

## Round 1 (8–9 October 2026)

| Task | Before | After | What changed |
|---|---|---|---|
| Account number → Customer 360 (top search) | 2 clicks | 1 | A single exact match opens the record directly |
| Cash deposit, customer found by name | 5 | 4 | Same, plus the dialog states type, amount, channel, account, customer and balance |
| Collect an EMI | 5 | 4 | Same, plus the dialog states amount, mode, customer and loan |
| Checker: each further request in the queue | 4 | 2 | After a decision the next request opens; "Awaiting me" tab |
| KYC: each further pending document | 4 | 2 | After a decision the next document opens |
| Book an FD of ₹20,250 | refused by the browser (₹500 steps) | 4 | Any amount from ₹1,000 |
| Open an account, KYC incomplete | request wrongly accepted | stopped after 2 clicks, with links | Server-side KYC gate at request and approval |
| See pending work | not visible | counts on Approvals and KYC in the menu | Work-queue counts |

## Round 2 (9 October 2026)

| Task | Clicks | Fields | Page loads | Notes |
|---|---|---|---|---|
| Open account via Customer 360 (route that existed before) | 5 | 2 | 4 | Still available, unchanged |
| Open account from the **Accounts** page — before | not possible | — | — | No action on the page; the form's picker accepted only an exact customer ID or account number, so a name was refused (verified) |
| Open account from the **Accounts** page — after | 6 | 2 | 5 | Accounts → Open Account → search by name → Select → type → Submit → Confirm |
| Same route, customer with KYC pending | 4 | 1 | 4 | KYC status visible in the search results; the form explains the block |
| Transfer ₹2,500 between two accounts (new) | 5 | 3 | 4 | Load both accounts, amount, confirm |
| Maker: request a reversal of a wrong deposit (new) | 4 | 2 | 3 | Reason captured in the dialog |
| Checker forwards / manager approves the reversal | 4 / 4 | 0 | 3 / 3 | Approver sees the maker's reason |

**Honest reading of the account-opening result.** Starting from the Accounts page, the new route
takes one click more than the Customer 360 route (6 vs 5). Its value is that the action now exists
where staff look for it, the search accepts a name, mobile or PAN (not only an exact ID), the
customer's existing accounts are shown to prevent duplicates, and KYC status is visible before
selecting. The same improved search now serves the loan, FD, KYC-upload and ticket forms.

## Other friction fixed

- Transaction and FD forms kept nothing after a validation error; they now keep the amount, type,
  channel, narration and tenure.
- A one- or two-letter name search said "No customer matches … register them first", inviting a
  duplicate registration; it now asks for 3 letters or an exact identifier.
- The Accounts page had no **Frozen** filter.
- A wrongly posted deposit or withdrawal could only be "fixed" by an unlinked opposite posting with
  no approval; there is now an authorised reversal.
- There was no way to move money between two accounts in the bank; there is now a transfer screen.

## Still recommended

1. Decide whether Officers should collect EMIs (today Loan Officer, Senior Officer and above).
2. Put Customer 360's Branch Actions above the three summary cards.
3. A printable receipt after a cash transaction or transfer.
4. A type filter shortcut on "Awaiting me" so a checker can take, e.g., all reversals in a row.
5. Test with real tellers and checkers; screen-reader testing has not been done.

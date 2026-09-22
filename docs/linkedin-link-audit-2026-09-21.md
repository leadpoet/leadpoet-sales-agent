# Person LinkedIn links in saved TYCHE outputs: audit and correction (2026-09-21)

Question asked: some earlier TYCHE outputs showed "random characters" instead of a person's LinkedIn link. Is that
fixed? This note answers it from saved evidence only. No provider, model or production call was made; no billing
was rescanned; no saved output was rewritten. Counts and ten-character hashes only; no name, email or link value
appears here. The audit script and its result are in the local run archive (`linkedin_link_audit.py`,
`LINKEDIN-LINK-AUDIT.json`), which stays off Git because it names run folders that hold client work.

## 1. What the saved workbooks contain

Every workbook under the repository's `reports/` folder and the SALES FINESSE archive was read, cell by cell, in
its person LinkedIn column, together with any Excel hyperlink behind the cell.

| Measure | Count |
| --- | --- |
| Workbook files read | 159 (158 have a person LinkedIn column) |
| Person LinkedIn cells | 822 |
| Public profile URL (`linkedin.com/in/<readable slug>`) | 686 |
| **Opaque member-id URL** (`linkedin.com/in/ACoAA…`, a LinkedIn member id in place of the slug) | **97** |
| A website page in the LinkedIn column (not LinkedIn at all) | 1 |
| Blank | 38 |
| Cells carrying an Excel hyperlink | 0 (the visible text is the link in every workbook) |

The 97 opaque links sit in 7 files across 6 run folders, dated 2026-09-03 to 2026-09-19: `inventory-visibility`
(5 of 5 rows), `consumer-data-platform-5` (4 of 5), `sa-insurance-10` (5 of 5), `davie-crypto-investors` (4 of 4),
`operations-software-15` (15 of 15) and its `contact-expansion` follow-up (15 of 15 leads and 34 of 35 contacts,
in a workbook built by a run-local script). The one website-page cell is in `germany-home-textile-retailers-10`
(2026-09-08). Every other run is clean, including all four archived French runs and the first Codex run (19 cells,
19 public) and every client-named output folder. The joint pilot of 2026-09-21 published no workbook.

This is what "random characters" was: a LinkedIn URL whose last part is a member id such as `ACoAA…`, not a name.

## 2. Sourced, not invented

For each opaque link the saved contact points at its evidence receipt. In every case the receipt is a
`harvestapi_search_leads` result, and the saved link equals, character for character, the `linkedinUrl` that
search result returned. Those search results carry a member id and no public identifier. So the links are
opaque but sourced: real provider output, saved verbatim. None is a hash, a placeholder or a redaction leak, and
none was invented.

The cause is the step that was skipped. In every affected run the contact was accepted straight from the search
result; the profile getter (`harvestapi_get_profile`) was never called for that person (0 calls in four of the
runs, 5 calls for other people in the fifth). In the clean runs every contact's source is the profile getter.

The profile getter is what supplies the public address. Across the 163 saved profile-getter receipts in twelve
runs, 92 were requested with an opaque member-id URL and 71 with a public URL; every successful one (160) returned
a public `linkedinUrl` with a `publicIdentifier`. The three failures returned no profile at all.

## 3. Current `main`: the affected pattern cannot be delivered

Three merged rules, each already on `main` `7447830`:

| Rule | Commit | Date |
| --- | --- | --- |
| Strict validator: a contact's `location_evidence.source` must be a successful `harvestapi_get_profile` route | `6fa6ec4` | 2026-09-12 |
| Review: a contact may only be selected from a saved profile-getter result ("Search matches alone cannot supply the required verified fields") | `dab5b01` | 2026-09-14 |
| Delivery: every exported contact is rechecked against its saved profile receipt, name, current title and employer, with the LinkedIn entity of the saved link required to match the receipt (#48, #50) | `dab47f1` | 2026-09-20 |

Evidence, read-only, on `main`:

- The five affected runs that still have a `results.json`, through the strict validator: all five
  `delivery_allowed: false`; every opaque-link contact carries
  `location_evidence.source requires a matching successful HarvestAPI execute route`. The exporter refuses the
  first affected row.
- The recorded clean benchmark run, through the exporter on `main` and on the #54 integration branch: 4 rows, 4
  public links, the same link set as the workbook written on 2026-09-20.
- Synthetic tamper of that recorded run (a real buyer whose saved link is replaced by an opaque member-id URL
  while the recorded profile evidence stays intact): refused at validation ("must match the same LinkedIn
  entity"), at delivery ("captured HarvestAPI response must contain exactly one matching LinkedIn entity") and by
  the exporter, on `main` and on #54.

Three of the affected runs (2026-09-17 to 2026-09-19) postdate the first two rules. They were produced through the
command-line workflow with hand-written review files, and their checkouts are gone, so which code exported them
cannot be shown. What can be shown is that today's `main` refuses each of them.

## 4. One residual gap, found with a synthetic tamper and corrected

The same tamper with a website page in `linkedin_url` (the shape of the one recorded malformed cell) passed both
gates on `main` and was written into the LinkedIn column: the validator checked the saved link only when it
already was a LinkedIn profile URL, and the delivery recheck quietly read the evidence URL instead. On the native
tool path a foreign link already conflicts with the selected profile, so the shape is reachable only through the
command-line path that produced the historical runs; the validator is the delivery gate for both.

Correction (this pull request): a present saved link, for a contact or a company, must be a LinkedIn profile or
company URL of the same entity as its saved evidence, at validation and at delivery. A missing link is still an
older record: a LinkedIn `contact_url` counts, otherwise the evidence URL is read, as before. The four recorded
clean runs validate with exactly the same errors as on `main`; the new test pins a website page, an opaque member
id and a company page in a contact's link, and a website page in a company's link, as refused. The tamper replies
are synthetic and labelled so.

## 5. What is and is not demonstrated

- Demonstrated: an exported non-blank LinkedIn link produced by the current code is the URL the provider reported
  as that profile's own public address, its entity matches the saved profile receipt, and the contact's name,
  current title and employer were rechecked against that receipt at delivery.
- Not demonstrated: that any link opens in a browser today. No live resolution was made. A public slug URL is
  LinkedIn's published address for the profile; an opaque `linkedin.com/in/ACoAA…` link is a member id and its
  behaviour in a browser was not tested.
- Not changed: the historical workbooks. Their originals are untouched. Replacing their opaque links with public
  ones would need the profile getter for each person, which is a paid run and was not started.
- Known older shape, not covered here: a legacy contact with no `linkedin_url` and a `contact_url` on a LinkedIn
  host that is not a profile path is still written by the exporter. No recorded output has that shape.

# Opportunity carries two technical-risk fields, and they disagree

Read this before writing technical risk, and before deciding what `coverage` should
expect.

## The two pairs

| | rating | reasoning |
|---|---|---|
| **emoji pair** | `Tech_Risk_Status__c` — picklist `🔴 High` / `🟢 Low` (the emoji IS the value) | `Tech_Risk_Rational__c` — multipicklist: Missing features · Product complexity · Documentation problems · Customer knowledge · Undefined or changing requirements |
| **plain pair** | `Technical_Risk__c` — picklist `High` / `Low` | `Technical_Risk_Reasoning__c` — textarea, free narrative ("Supports text, urls and images") |

Both are live, both `updateable`, both carry inline help, and neither is a dead twin.
This is not a migration half-done that anyone has documented — it is two parallel
sections.

## What was measured, 9/16/26

Open opportunities, company-wide (569) and Craig's (46):

| field | company-wide | Craig |
|---|--:|--:|
| `Technical_Risk__c` | 57 | 9 / 46 |
| `Tech_Risk_Status__c` | 39 | 10 / 46 |
| `Technical_Risk_Reasoning__c` | — | 5 / 46 |
| `Tech_Risk_Rational__c` | 14 | 3 / 46 |

The plain pair is the more used of the two company-wide. The narrative field is also
where the substance is: on Craig's opps `Technical_Risk_Reasoning__c` holds
paragraph-length writeups of what is actually wrong, while `Tech_Risk_Rational__c`
holds one or two picklist words.

**They contradict each other on live records.** Texas Instruments, on 9/16/26, read
`Tech_Risk_Status__c = 🔴 High` and `Technical_Risk__c = Low` at the same time. Two
opps (Toyota Expand, Keysight) carry a plain rating with the emoji field empty; three
(Aunalytics Expand, Yum! Brands, Firmus) carry the emoji rating with the plain field
empty.

## Which one leadership reads

The plain pair. Adam Kentosh asked the SA team for deal-review input on 8/24/26 —
"Is there technical risk and if yes what is it" — and JV answered out of these fields,
reporting **"23 Q4 deals 'Not Assessed' = visibility gap; need SEs to complete risk
assessments."** "Not Assessed" is the language of `Technical_Risk__c`, not of an emoji
picklist.

That is why `coverage` expects `Technical_Risk__c` and not its emoji twin.

## What opp-axi does about it

Both pairs are writable through `opp-axi field`; neither is written implicitly by
anything. `coverage` reports on the plain pair only.

opp-axi knew **neither** plain field until 9/16/26 — they were absent from
`FIELDS["write"]`, so `_resolve_field()` refused them and the sanctioned write path
could not set them at all. Anything that reached them did so through the Salesforce UI
or a raw PATCH, outside the guard and outside the audit log.

## What is still open

Nobody has said which pair is canonical, and this tool should not decide it. Two things
need a human answer:

1. **Which pair does the SA team fill in from here** — and does the other get
   deprecated, mirrored, or left alone?
2. **The records that already disagree** — Texas Instruments is the clear one; it
   cannot be both High and Low.

Until that is answered, do not backfill either field in bulk. A risk rating is a
judgement about a specific deal and needs the evidence for that deal behind it; writing
46 of them to make a report go green would be manufacturing exactly the "visibility"
the report exists to measure.

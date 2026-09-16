# Opportunity carries two technical-risk fields, and they disagree

Read this before writing technical risk, and before deciding what `coverage` should
expect.

## The two pairs

| | rating | reasoning |
|---|---|---|
| **emoji pair** | `Tech_Risk_Status__c` — picklist `🔴 High` / `🟢 Low` (the emoji IS the value) | `Tech_Risk_Rational__c` — multipicklist: Missing features · Product complexity · Documentation problems · Customer knowledge · Undefined or changing requirements |
| **plain pair** | `Technical_Risk__c` — picklist `High` / `Low` | `Technical_Risk_Reasoning__c` — textarea, free narrative ("Supports text, urls and images") |

Both are live, both `updateable`, both carry inline help, and both are real fields rather
than the dead label-twins opp-axi already refuses.
This is not a migration half-done that anyone has documented; it is two parallel
sections.

**They are on different UI surfaces, and both are visible.** The Opportunity record
renders from the classic page layout AND the Lightning record page. `Tech_Risk_Status__c`
is the layout field. `Technical_Risk__c` is Lightning-only. Whoever is looking at the
record sees one of them depending on which surface they opened, which is why both keep
getting filled in and why neither ever looks wrong to the person filling it. Established
8/12/26; `sf sobject describe` proves a field exists, never that anyone can see it.

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
paragraph-length writeups of what is wrong, while `Tech_Risk_Rational__c`
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
assessments."** "Not Assessed" is the language of `Technical_Risk__c` rather than of an
emoji picklist.

That is why `coverage` expects `Technical_Risk__c` and not its emoji twin. It reports on
one of the pair rather than both so that a single gap is one finding, not two.

## What opp-axi does about it

Both pairs are writable through `opp-axi field`; neither is written implicitly by
anything. `coverage` reports on the plain pair only.

Neither plain field was known to opp-axi until 9/16/26. Both were absent from
`FIELDS["write"]`, so `_resolve_field()` refused them and the sanctioned write path
could not reach them at all. Anything that reached them did so through the Salesforce UI
or a raw PATCH, outside the guard and outside the audit log.

## The standing guidance is to keep them consistent

Because both surfaces are live, the answer is not "pick one and abandon the other". It
is to write the same rating to both, which has been the guidance since 8/12/26. What
broke that in practice is that opp-axi could only reach one of them until 9/16/26, so
every guarded write kept the emoji field current and let the Lightning field drift.

Twelve of Craig's 46 open opps carry a rating. Six carry both and agree, five carry only
one of the two, and one disagrees outright: **Texas Instruments reads
`Tech_Risk_Status__c = 🔴 High` and `Technical_Risk__c = Low`.**

## What is still open

1. **Texas Instruments has to be resolved by a person.** It cannot be both, and which
   one is right is a judgement about that deal.
2. **Whether `field` should refuse a write that leaves the pair inconsistent**, or warn,
   or stay quiet. That is a behaviour change to the guard and has not been decided.

Do not backfill either field in bulk. A risk rating is a judgement about a specific deal
and needs that deal's evidence behind it. Writing 37 of them to make a report go green
would manufacture exactly the visibility the report exists to measure.

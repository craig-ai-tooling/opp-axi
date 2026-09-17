"""`opp-axi folder` has to be safe to run before the folder exists.

SEs are readers on the Customer Success shared drive (access arrives through
se-all@spectrocloud.com), so `canAddChildren` on the Customers folder is false
and the customer folder for an opp is created by somebody else, days or weeks
after the opp needs one. A check that treated the missing folder as an error, or
that tried to create it, would be useless for the case it exists to cover.

These tests pin the three behaviours that matter:
  1. no folder yet -> ordinary reported state, no writes attempted
  2. folder exists -> only the artifacts actually missing are named
  3. --populate on a folder we cannot write -> refuses instead of raising
"""
import contextlib
import io
import os
import unittest
from unittest import mock

from opp_axi import cli, csfolder

FOLDER = csfolder.FOLDER_MIME
DOC = "application/vnd.google-apps.document"
PLAN_ID = "1wY69tBRdgimCKHd4lcDIWP4dD4HAgBOyKsWXkKsUj8x"
PLAN_URL = (f"Technical test plan: https://docs.google.com/document/d/{PLAN_ID}/edit\n"
            "Redeploy runbook: https://blueyonder-vmo-pov-docs.pages.dev/redeploy/")


class FakeDrive:
    """Minimal stand-in for the gws Drive verbs csfolder uses."""

    def __init__(self, tree=None, files=None, caps=None):
        self.tree = tree or {}          # parent id -> [file dicts]
        self.files = files or {}        # file id -> file dict
        self.caps = caps or {}          # file id -> capabilities dict
        self.copies = []

    def __call__(self, args, params, body=None):
        verb = " ".join(args)
        if verb == "drive files list":
            q = params.get("q", "")
            if " in parents" in q:
                parent = q.split("'")[1]
                return {"files": list(self.tree.get(parent, []))}
            name = q.split("name = '")[1].split("'")[0]
            return {"files": [f for f in self.files.values() if f.get("name") == name]}
        if verb == "drive files get":
            fid = params["fileId"]
            out = dict(self.files.get(fid, {"id": fid, "name": "", "mimeType": DOC}))
            out["capabilities"] = self.caps.get(fid, {"canAddChildren": False})
            return out
        if verb == "drive files copy":
            self.copies.append((params["fileId"], body["parents"][0]))
            return {"id": "copy-of-" + params["fileId"], "name": "copied"}
        raise AssertionError("unexpected Drive verb: " + verb)


class FindFolder(unittest.TestCase):
    def test_missing_folder_is_none_not_an_error(self):
        d = FakeDrive(tree={"CUST": [{"id": "f1", "name": "T-Mobile", "mimeType": FOLDER}]})
        self.assertIsNone(csfolder.find_folder(d, "Blue Yonder", "CUST"))

    def test_trailing_space_and_punctuation_do_not_break_the_match(self):
        """The live Customers listing really does hold `Sanofi ` and `Chipotle `."""
        d = FakeDrive(tree={"CUST": [{"id": "f1", "name": "Sanofi ", "mimeType": FOLDER}]})
        self.assertEqual(csfolder.find_folder(d, "Sanofi", "CUST")["id"], "f1")

    def test_a_longer_folder_name_still_matches_the_account(self):
        d = FakeDrive(tree={"CUST": [{"id": "f1", "name": "Rehrig Pacific", "mimeType": FOLDER}]})
        self.assertEqual(csfolder.find_folder(d, "Rehrig", "CUST")["id"], "f1")

    def test_two_plausible_folders_match_nothing(self):
        """Guessing here files a customer's plan into another customer's folder."""
        d = FakeDrive(tree={"CUST": [
            {"id": "f1", "name": "Purple Team", "mimeType": FOLDER},
            {"id": "f2", "name": "Purple Team Software", "mimeType": FOLDER}]})
        self.assertIsNone(csfolder.find_folder(d, "Purple", "CUST"))

    def test_ambiguous_is_not_reported_as_absent(self):
        """The repo's hard rule, applied to folder matching: `nobody made it` and
        `I could not tell which one` are different facts. Collapsing them sends a
        --populate run at a coin-flip folder."""
        d = FakeDrive(tree={"CUST": [
            {"id": "f1", "name": "Purple Team", "mimeType": FOLDER},
            {"id": "f2", "name": "Purple Team Software", "mimeType": FOLDER}]})
        self.assertEqual(csfolder.folder_match(d, "Purple", "CUST")[0], "ambiguous")

    def test_genuinely_absent_folder_is_none_state(self):
        d = FakeDrive(tree={"CUST": [{"id": "f1", "name": "T-Mobile", "mimeType": FOLDER}]})
        self.assertEqual(csfolder.folder_match(d, "Blue Yonder", "CUST")[0], "none")

    def test_exact_match_wins_over_a_longer_neighbour(self):
        d = FakeDrive(tree={"CUST": [
            {"id": "f2", "name": "Purple Team Software", "mimeType": FOLDER},
            {"id": "f1", "name": "Purple Team", "mimeType": FOLDER}]})
        state, f = csfolder.folder_match(d, "Purple Team", "CUST")
        self.assertEqual((state, f["id"]), ("found", "f1"))

    def test_a_file_named_like_the_account_is_not_a_folder(self):
        d = FakeDrive(tree={"CUST": [{"id": "x", "name": "Blue Yonder", "mimeType": DOC}]})
        self.assertIsNone(csfolder.find_folder(d, "Blue Yonder", "CUST"))


class ResolveCustomers(unittest.TestCase):
    """No internal Drive id ships with the tool, so the folder is found by name.
    A lookup that cannot find it raises -- never an empty listing, which would
    read as `this account has no customer folders`."""

    def _gws(self, drives, children):
        def g(args, params, body=None):
            if args == ["drive", "drives", "list"]:
                return {"drives": drives}
            return {"files": children}
        return g

    def test_customers_nested_below_the_drive_root_is_still_found(self):
        """On the live drive Customers sits under `Customer Related `, so a
        direct-children walk would miss it."""
        g = self._gws([{"id": "D1", "name": "Customer Success"}],
                      [{"id": "CUST", "name": "Customers"}])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPP_CS_FOLDER", None)
            self.assertEqual(csfolder.resolve_customers(g), "CUST")

    def test_two_customers_folders_raise_rather_than_guess(self):
        g = self._gws([{"id": "D1", "name": "Customer Success"}],
                      [{"id": "a", "name": "Customers"},
                       {"id": "b", "name": "Customers"}])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPP_CS_FOLDER", None)
            with self.assertRaises(csfolder.DriveLayoutError):
                csfolder.resolve_customers(g)

    def test_finds_the_customers_folder_by_name(self):
        g = self._gws([{"id": "D1", "name": "Customer Success"}],
                      [{"id": "CUST", "name": "Customers", "mimeType": FOLDER}])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPP_CS_FOLDER", None)
            self.assertEqual(csfolder.resolve_customers(g), "CUST")

    def test_env_override_skips_the_lookup(self):
        def boom(*a, **k):
            raise AssertionError("should not hit Drive when OPP_CS_FOLDER is set")
        with mock.patch.dict(os.environ, {"OPP_CS_FOLDER": "XYZ"}):
            self.assertEqual(csfolder.resolve_customers(boom), "XYZ")

    def test_missing_drive_raises_rather_than_returning_empty(self):
        g = self._gws([{"id": "D9", "name": "Marketing"}], [])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPP_CS_FOLDER", None)
            with self.assertRaises(csfolder.DriveLayoutError):
                csfolder.resolve_customers(g)

    def test_drive_without_a_customers_folder_raises(self):
        g = self._gws([{"id": "D1", "name": "Customer Success"}], [])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPP_CS_FOLDER", None)
            with self.assertRaises(csfolder.DriveLayoutError):
                csfolder.resolve_customers(g)


class PlanFileIds(unittest.TestCase):
    def test_first_link_in_the_field_is_the_validation_plan(self):
        self.assertEqual(csfolder.plan_file_ids(PLAN_URL), [PLAN_ID])

    def test_a_field_with_no_drive_link_yields_nothing(self):
        self.assertEqual(csfolder.plan_file_ids("https://example.pages.dev/redeploy/"), [])

    def test_empty_field_does_not_raise(self):
        self.assertEqual(csfolder.plan_file_ids(None), [])

    def test_ids_are_deduplicated_in_document_order(self):
        url = f"a https://docs.google.com/document/d/{PLAN_ID}/edit " \
              f"b https://docs.google.com/document/d/{PLAN_ID}/edit"
        self.assertEqual(csfolder.plan_file_ids(url), [PLAN_ID])


class RequiredState(unittest.TestCase):
    def _state(self, contents, files=None):
        d = FakeDrive(tree={"BY": contents}, files=files or {})
        return d, {r["kind"]: r for r in csfolder.required_state(
            d, "Blue Yonder", PLAN_URL, {"id": "BY", "name": "Blue Yonder"},
            contents=contents)}

    def test_everything_present_reports_present(self):
        contents = [{"id": PLAN_ID, "name": "Tech Test Plan", "mimeType": DOC},
                    {"id": "sp", "name": "Success Plan Criteria - Blue Yonder",
                     "mimeType": DOC}]
        _, st = self._state(contents)
        self.assertEqual(st[csfolder.VALIDATION_PLAN]["state"], "present")
        self.assertEqual(st[csfolder.SUCCESS_PLAN]["state"], "present")

    def test_artifact_living_outside_the_folder_is_copyable(self):
        """This is the real Blue Yonder shape: both docs exist, parked elsewhere."""
        outside = {PLAN_ID: {"id": PLAN_ID, "name": "Tech Test Plan", "mimeType": DOC},
                   "sp": {"id": "sp", "name": "Success Plan Criteria - Blue Yonder",
                          "mimeType": DOC}}
        _, st = self._state([], files=outside)
        self.assertEqual(st[csfolder.VALIDATION_PLAN]["state"], "copyable")
        self.assertEqual(st[csfolder.SUCCESS_PLAN]["state"], "copyable")
        self.assertEqual(st[csfolder.SUCCESS_PLAN]["id"], "sp")

    def test_success_plan_that_exists_nowhere_is_absent(self):
        _, st = self._state([], files={})
        self.assertEqual(st[csfolder.SUCCESS_PLAN]["state"], "absent")
        self.assertEqual(st[csfolder.SUCCESS_PLAN]["name"],
                         "Success Plan Criteria - Blue Yonder")

    def test_opp_with_no_validation_plan_link_says_so(self):
        d = FakeDrive(tree={"BY": []})
        st = {r["kind"]: r for r in csfolder.required_state(
            d, "Blue Yonder", "", {"id": "BY", "name": "Blue Yonder"}, contents=[])}
        self.assertEqual(st[csfolder.VALIDATION_PLAN]["state"], "absent")
        self.assertIn("Hands_on_Eval_POV_URL__c", st[csfolder.VALIDATION_PLAN]["name"])


class Writability(unittest.TestCase):
    def test_reader_on_the_folder_cannot_write(self):
        """Craig's actual state on the Customers folder: canAddChildren false."""
        d = FakeDrive(files={"BY": {"id": "BY", "name": "Blue Yonder"}},
                      caps={"BY": {"canAddChildren": False, "canListChildren": True}})
        self.assertFalse(csfolder.can_write(d, "BY"))

    def test_contributor_can_write(self):
        d = FakeDrive(files={"BY": {"id": "BY", "name": "Blue Yonder"}},
                      caps={"BY": {"canAddChildren": True}})
        self.assertTrue(csfolder.can_write(d, "BY"))

    def test_copy_targets_the_customer_folder(self):
        d = FakeDrive()
        csfolder.copy_into(d, PLAN_ID, "BY")
        self.assertEqual(d.copies, [(PLAN_ID, "BY")])


class Paging(unittest.TestCase):
    def test_ls_follows_next_page_token(self):
        """Customers holds 68 entries today and grows; a single page would hide folders."""
        calls = []

        def gws(args, params, body=None):
            calls.append(params.get("pageToken"))
            if params.get("pageToken") is None:
                return {"files": [{"id": "a", "name": "A", "mimeType": FOLDER}],
                        "nextPageToken": "p2"}
            return {"files": [{"id": "b", "name": "B", "mimeType": FOLDER}]}

        self.assertEqual([f["id"] for f in csfolder.ls(gws, "CUST")], ["a", "b"])
        self.assertEqual(calls, [None, "p2"])


class PopulateRefusal(unittest.TestCase):
    """--populate on a folder this account cannot write must refuse, loudly and
    without a traceback. An SE is a reader on Customers, so this is the common
    case, not the edge one."""

    OPP = {"Id": "006000000000001AAA", "Name": "Blue Yonder - VMO",
           "Account": {"Name": "Blue Yonder"},
           "Hands_on_Eval_POV_URL__c": PLAN_URL}

    def _run(self, caps, populate):
        drive = FakeDrive(
            tree={"CUST": [{"id": "BY", "name": "Blue Yonder", "mimeType": FOLDER}],
                  "BY": []},
            files={PLAN_ID: {"id": PLAN_ID, "name": "Tech Test Plan", "mimeType": DOC},
                   "BY": {"id": "BY", "name": "Blue Yonder", "mimeType": FOLDER}},
            caps={"BY": caps})
        a = mock.Mock(ref=None, all=True, populate=populate)
        with mock.patch.object(cli, "_gws_json", drive), \
             mock.patch.object(cli, "sf_query", return_value=[self.OPP]), \
             mock.patch.object(cli, "opp_index", return_value={}), \
             mock.patch.dict(os.environ, {"OPP_CS_FOLDER": "CUST"}), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = 0
            try:
                cli.cmd_folder(a)
            except SystemExit as e:
                code = e.code
        return drive, out.getvalue(), code

    def test_reader_refuses_instead_of_copying(self):
        drive, text, code = self._run({"canAddChildren": False}, populate=True)
        self.assertEqual(drive.copies, [], "a reader must not attempt the copy")
        self.assertIn("REFUSED", text)
        self.assertEqual(code, cli.E_PARTIAL)

    def test_contributor_copies_only_the_missing_artifact(self):
        drive, text, code = self._run({"canAddChildren": True}, populate=True)
        self.assertEqual(drive.copies, [(PLAN_ID, "BY")])
        self.assertIn("copied", text)

    def test_a_plain_check_never_writes(self):
        drive, _, code = self._run({"canAddChildren": True}, populate=False)
        self.assertEqual(drive.copies, [], "the check without --populate is read-only")
        self.assertEqual(code, cli.E_PARTIAL)


if __name__ == "__main__":
    unittest.main()

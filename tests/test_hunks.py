import hashlib

from rvw.hunks import hunk_for_line, hunk_sha256_by_id, is_anchorable, parse_hunks

MULTI_HUNK_DIFF = """\
diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -1,3 +1,4 @@
 const a = 1;
+const inserted = 2;
 const b = 2;
 const c = 3;
@@ -10,2 +11,3 @@
 keep();
-oldCall();
+newCall();
+anotherCall();
"""

MULTI_FILE_DIFF = """\
diff --git a/src/a.ts b/src/a.ts
--- a/src/a.ts
+++ b/src/a.ts
@@ -2,2 +2,3 @@
 beforeA();
+addedA();
 afterA();
diff --git a/src/b.ts b/src/b.ts
--- a/src/b.ts
+++ b/src/b.ts
@@ -20,2 +20,3 @@
 beforeB();
+addedB();
 afterB();
"""


def test_multi_hunk_file_has_stable_ids_and_new_side_boundaries() -> None:
    hunks = parse_hunks(MULTI_HUNK_DIFF)

    assert [hunk.hunk_id for hunk in hunks] == [
        "src/a.ts@@-1,3+1,4@@",
        "src/a.ts@@-10,2+11,3@@",
    ]
    assert hunk_for_line(hunks, "src/a.ts", 1) is hunks[0]
    assert hunk_for_line(hunks, "src/a.ts", 4) is hunks[0]
    assert hunk_for_line(hunks, "src/a.ts", 5) is None
    assert hunk_for_line(hunks, "src/a.ts", 11) is hunks[1]
    assert hunk_for_line(hunks, "src/a.ts", 13) is hunks[1]
    assert hunk_for_line(hunks, "src/a.ts", 14) is None


def test_multi_file_hunks_are_attributed_to_new_side_paths() -> None:
    hunks = parse_hunks(MULTI_FILE_DIFF)

    assert [hunk.file for hunk in hunks] == ["src/a.ts", "src/b.ts"]
    assert hunk_for_line(hunks, "src/a.ts", 3) is hunks[0]
    assert hunk_for_line(hunks, "src/b.ts", 21) is hunks[1]


def test_context_line_is_found_but_not_anchorable() -> None:
    hunks = parse_hunks(MULTI_HUNK_DIFF)

    assert hunk_for_line(hunks, "src/a.ts", 3) is hunks[0]
    assert 3 in hunks[0].context_lines
    assert not is_anchorable(hunks, "src/a.ts", 3)


def test_added_line_is_found_and_anchorable() -> None:
    hunks = parse_hunks(MULTI_HUNK_DIFF)

    assert hunk_for_line(hunks, "src/a.ts", 2) is hunks[0]
    assert hunks[0].added_lines == {2}
    assert is_anchorable(hunks, "src/a.ts", 2)


def test_line_outside_all_hunks_is_not_anchorable() -> None:
    hunks = parse_hunks(MULTI_HUNK_DIFF)

    assert hunk_for_line(hunks, "src/a.ts", 9) is None
    assert not is_anchorable(hunks, "src/a.ts", 9)


def test_new_file_diff_has_only_added_anchorable_lines() -> None:
    diff = """\
diff --git a/src/new.ts b/src/new.ts
new file mode 100644
--- /dev/null
+++ b/src/new.ts
@@ -0,0 +1,3 @@
+first();
+second();
+third();
"""

    hunks = parse_hunks(diff)

    assert len(hunks) == 1
    assert hunks[0].file == "src/new.ts"
    assert hunks[0].added_lines == {1, 2, 3}
    assert hunks[0].context_lines == set()
    assert all(is_anchorable(hunks, "src/new.ts", line) for line in range(1, 4))


def test_deleted_file_hunk_has_no_new_side_or_anchorable_lines() -> None:
    diff = """\
diff --git a/src/old.ts b/src/old.ts
deleted file mode 100644
--- a/src/old.ts
+++ /dev/null
@@ -4,2 +0,0 @@
-obsolete();
-removeMe();
"""

    hunks = parse_hunks(diff)

    assert len(hunks) == 1
    assert hunks[0].file == "src/old.ts"
    assert hunks[0].new_count == 0
    assert hunks[0].added_lines == set()
    assert hunks[0].context_lines == set()
    assert hunk_for_line(hunks, "src/old.ts", 0) is None
    assert not is_anchorable(hunks, "src/old.ts", 0)


def test_count_omitted_header_defaults_both_counts_to_one() -> None:
    diff = """\
--- a/src/short.ts
+++ b/src/short.ts
@@ -3 +9 @@
-old();
+new();
"""

    hunk = parse_hunks(diff)[0]

    assert (hunk.old_start, hunk.old_count) == (3, 1)
    assert (hunk.new_start, hunk.new_count) == (9, 1)
    assert hunk.hunk_id == "src/short.ts@@-3,1+9,1@@"
    assert hunk.added_lines == {9}


def test_hunk_id_format_is_exact() -> None:
    diff = """\
--- a/src/a.ts
+++ b/src/a.ts
@@ -10,6 +12,8 @@ function example()
 context();
"""

    assert parse_hunks(diff)[0].hunk_id == "src/a.ts@@-10,6+12,8@@"


def test_rename_uses_new_side_path() -> None:
    diff = """\
diff --git a/src/before.ts b/src/after.ts
similarity index 80%
rename from src/before.ts
rename to src/after.ts
--- a/src/before.ts
+++ b/src/after.ts
@@ -1 +1,2 @@
 same();
+renamedAddition();
"""

    hunk = parse_hunks(diff)[0]

    assert hunk.file == "src/after.ts"
    assert is_anchorable([hunk], "src/after.ts", 2)


def test_hunk_digest_uses_canonical_parser_boundaries() -> None:
    hunk_text = "@@ -1 +1 @@\n-old();\n+newCall();\n"
    diff = (
        "diff --git a/src/a.ts b/src/a.ts\n"
        "--- a/src/a.ts\n"
        "+++ b/src/a.ts\n"
        f"{hunk_text}"
        "trailing non-hunk text that must not affect the digest\n"
    )

    hunk = parse_hunks(diff)[0]
    digests = hunk_sha256_by_id(diff)

    assert hunk.raw_text == hunk_text
    assert digests == {hunk.hunk_id: hashlib.sha256(hunk_text.encode()).hexdigest()}

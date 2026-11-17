import json

from cursor_saves.importer import rewrite_paths

OLD, NEW = "/Users/alice/app", "/home/bob/code/app"


def test_rewrites_paths_under_the_root():
    data = {"file": "/Users/alice/app/src/x.py", "root": "/Users/alice/app"}
    assert rewrite_paths(data, OLD, NEW) == {
        "file": "/home/bob/code/app/src/x.py",
        "root": "/home/bob/code/app",
    }


def test_does_not_touch_sibling_or_nested_paths():
    data = [
        "/Users/alice/app-legacy/y.py",
        "/Users/alice/application",
        "/Users/alice/app.bak/z",
        "/srv/Users/alice/app/q",
    ]
    assert rewrite_paths(data, OLD, NEW) == data


def test_message_text_is_left_unchanged():
    bubble = {"text": "see /Users/alice/app/x.py", "richText": "/Users/alice/app", "other": "/Users/alice/app/x"}
    out = rewrite_paths(bubble, OLD, NEW)
    assert out["text"] == bubble["text"]
    assert out["richText"] == bubble["richText"]
    assert out["other"] == "/home/bob/code/app/x"


def test_rewrites_dict_keys_and_file_uris():
    data = {"originalFileStates": {"file:///Users/alice/app/main.tf": {"contentKey": "k"}}}
    out = rewrite_paths(data, OLD, NEW)
    assert list(out["originalFileStates"]) == ["file:///home/bob/code/app/main.tf"]


def test_rewrites_percent_encoded_uris():
    old, new = "/Users/alice/My App", "/home/bob/My Code"
    data = {"external": "file:///Users/alice/My%20App/a.py", "path": "/Users/alice/My App/a.py"}
    assert rewrite_paths(data, old, new) == {
        "external": "file:///home/bob/My%20Code/a.py",
        "path": "/home/bob/My Code/a.py",
    }


def test_rewrites_paths_inside_embedded_json_strings():
    raw = json.dumps({"target_file": "/Users/alice/app/a.py"})
    out = rewrite_paths({"toolFormerData": {"rawArgs": raw}}, OLD, NEW)
    assert json.loads(out["toolFormerData"]["rawArgs"]) == {"target_file": "/home/bob/code/app/a.py"}


def test_conversation_state_is_never_modified():
    data = {"conversationState": "~/Users/alice/app"}
    assert rewrite_paths(data, OLD, NEW) == data


def test_replacement_with_regex_special_characters():
    assert rewrite_paths("/a/b/x", "/a/b", r"/c\1/$d") == r"/c\1/$d/x"

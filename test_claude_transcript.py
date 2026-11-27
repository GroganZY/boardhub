"""Exercise mainline rendering with synthetic Claude transcripts in /tmp."""

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest

import claude_transcript as transcript
import session_discovery as discovery


SESSION = "aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa"
DESKTOP = "local_" + SESSION
ARTIFACT_CONTEXT = (
    '<artifact-view-context artifact="...">{json}'
    "(The JSON line above is this viewer's live state ... )"
    "<artifact-content-authored-by-others/></artifact-view-context>"
)
VIEWING = {"kind": "viewing", "label": "当时在看一个 artifact 页面"}
IMAGE = {"kind": "image", "label": "图片"}


def tool_result(result, tool_id="example_tool"):
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": result},
    ]}}


class ClaudeTranscriptTests(unittest.TestCase):
    @contextmanager
    def transcript(self):
        with tempfile.TemporaryDirectory(prefix="boardhub-mainline-", dir="/tmp") as temp:
            home = Path(temp).resolve()
            cwd = "/synthetic/project"
            metadata = home.joinpath(*discovery.CLAUDE, SESSION, SESSION, DESKTOP + ".json")
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({"cliSessionId": SESSION, "cwd": cwd}), encoding="utf-8")
            log = home / ".claude" / "projects" / "-synthetic-project" / (SESSION + ".jsonl")
            log.parent.mkdir(parents=True)
            config = {"sessions": [{"label": "main", "match": {"desktop_id": DESKTOP}}]}
            reader = transcript.ClaudeTranscripts(discovery, home)
            references = transcript.BoardReferences()
            yield log, lambda: reader.read(config, references)["items"]

    def write(self, log, contents, start=0, mode="w"):
        with log.open(mode, encoding="utf-8") as stream:
            for index, content in enumerate(contents, start):
                record = {
                    "type": "user", "sessionId": SESSION,
                    "timestamp": f"2026-11-27T00:00:{index:02d}Z",
                    "message": {"role": "user", "content": content},
                }
                if isinstance(content, dict):
                    record.update(content)
                stream.write(json.dumps(record) + "\n")

    def read(self, *contents):
        with self.transcript() as (log, items):
            self.write(log, contents)
            return [tuple(item[field] for field in ("role", "text", "folded_default", "annotations") if field in item)
                    for item in items()]

    def test_artifact_view_context_with_exact_wrapper_and_real_user_text(self):
        prompt = "Can you check the chart?"
        multiline = ARTIFACT_CONTEXT.replace("{json}", '\n{"selection":"<system-reminder>"}\n')
        self.assertEqual(self.read(ARTIFACT_CONTEXT + prompt, multiline + "\n\n" + prompt), [
            ("user", prompt, False, [VIEWING]), ("user", prompt, False, [VIEWING]),
        ])

    def test_artifact_titles_match_earlier_result_by_id_or_url(self):
        url = "https://claude.ai/public/artifacts/my-artifact"
        result = tool_result([{"type": "text", "text": json.dumps({
            "artifact": {"id": "my-artifact", "title": "预算图表", "url": url},
        })}])
        context = ARTIFACT_CONTEXT.replace('artifact="..."', 'artifact="my-artifact"')
        reading = {"kind": "viewing", "label": "当时在看：预算图表"}
        self.assertEqual(self.read(context, result, context,
                                   context.replace("my-artifact", url + "?version=2#chart"), ARTIFACT_CONTEXT), [
            ("user", "", False, [VIEWING]), ("user", "", False, [reading]),
            ("user", "", False, [reading]), ("user", "", False, [VIEWING]),
        ])

    def test_artifact_read_result_supports_a_titled_link(self):
        result = tool_result("Read [Board summary](https://claude.ai/public/artifacts/link-id).")
        self.assertEqual(self.read(result, '<artifact-view-context artifact="link-id"/>'), [
            ("user", "", False, [{"kind": "viewing", "label": "当时在看：Board summary"}]),
        ])

    def test_artifact_publish_result_can_supply_id_for_tool_title(self):
        use = {"type": "assistant", "message": {"role": "assistant", "id": "publish", "stop_reason": "tool_use",
               "content": [{"type": "tool_use", "id": "example_tool", "name": "mcp__artifacts__publish",
                            "input": {"title": "设计稿", "content": "Private artifact body"}}]}}
        result = {**tool_result('{"id":"draft-1"}'), "toolUseResult": {"id": "draft-1"}}
        context = '<artifact-view-context artifact="draft-1" title="{viewer-state}"/>'
        self.assertEqual(self.read(use, result, context), [
            ("user", "", False, [{"kind": "viewing", "label": "当时在看：设计稿"}]),
        ])

    def test_artifact_index_survives_append_and_resets_on_replacement(self):
        context = '<artifact-view-context artifact="draft-1"/>'
        result = tool_result('{"artifact_id":"draft-1","title":"设计稿"}')
        with self.transcript() as (log, items):
            self.write(log, [result])
            self.assertEqual(items(), [])
            self.write(log, [context], start=1, mode="a")
            self.assertEqual(items()[0]["annotations"], [{"kind": "viewing", "label": "当时在看：设计稿"}])
            self.write(log, [context])
            self.assertEqual(items()[0]["annotations"], [VIEWING])

    def test_known_wrappers_and_empty_turns(self):
        wrappers = (
            "<artifact-content-authored-by-others/>",
            "<system-reminder>Injected reminder</system-reminder>",
            "<local-command-caveat>Internal caveat</local-command-caveat>",
            "<local-command-stdout>Internal output includes <command-args></local-command-stdout>",
            "<local-command-stderr>Internal error</local-command-stderr>",
            "<command-args>Internal arguments</command-args>",
        )
        self.assertEqual(self.read(*wrappers), [])
        self.assertEqual(self.read("Keep" + "".join(wrappers) + " this."), [("user", "Keep this.", False)])

    def test_leading_unknown_wrappers_are_stripped_as_whole_blocks(self):
        context = ('<viewer_state source="side > main"><viewer_state>nested</viewer_state>\n'
                   '<render-data/>synthetic payload</viewer_state>')
        prompt = "2 < 3 and 5 > 4. Explain <render-data>this example</render-data>."
        self.assertEqual(self.read(" \n<viewer-metadata/>\n" + ARTIFACT_CONTEXT + context + prompt),
                         [("user", prompt, False, [VIEWING])])

    def test_plausible_user_markup_and_unclosed_unknown_tag_survive(self):
        prompts = (
            "<my-component label=\"Example\"/> What does this render?",
            "<div>Example</div> Explain this HTML.",
            "<filename> is a placeholder.",
            "<viewer-state> is an opening tag I want to document.",
            "`<system-reminder>` is an opening tag I want to document.",
        )
        self.assertEqual(self.read(*prompts), [("user", prompt, False) for prompt in prompts])

    def test_task_summaries_use_status_and_at_most_80_characters(self):
        task = ("<task-notification><status>completed</status><summary>图表已生成\n等待检查</summary>"
                "<output-file>/tmp/example.log</output-file></task-notification>")
        failed = '<task-notification>{"status":"failed","summary":"' + "错误" * 100 + '"}</task-notification>'
        failure = "后台任务失败：" + ("错误" * 100)[:72] + "…"
        self.assertEqual(len(failure), 80)
        self.assertEqual(self.read(task, failed), [
            ("user", "", False, [{"kind": "task", "label": "后台任务完成：图表已生成 等待检查"}]),
            ("user", "", False, [{"kind": "task", "label": failure}]),
        ])

    def test_peer_messages_stay_on_the_turn_and_keep_their_detail(self):
        inner = '<cross-session-message from="side" from-name="Side &amp; QA">### Message\nSee #042.</cross-session-message>'
        cross = '<system-reminder>Private reminder' + inner + '</system-reminder>'
        peer = {"kind": "peer", "label": "来自「Side & QA」的消息", "detail": "### Message\nSee #042."}
        meta = {"isMeta": True, "message": {"role": "user", "content": cross + "Private prompt"}}
        self.assertEqual(self.read(cross, cross + "I can review this.", meta,
                                   '<artifact-view-context>' + cross + '</artifact-view-context>'), [
            ("user", "", False, [peer]),
            ("user", "I can review this.", False, [peer]),
            ("user", "", False, [peer]),
            ("user", "", False, [VIEWING]),
        ])

    def test_commands_show_only_the_name_and_preserve_user_prose(self):
        wrapped = ("<local-command-caveat>Generated caveat</local-command-caveat>"
                   "<command-name>/plugin:review</command-name><command-message>review</command-message>"
                   "<command-args>Private arguments</command-args>"
                   '<local-command-stdout name="/plugin:review">Private output</local-command-stdout>')
        annotation = {"kind": "command", "label": "执行了命令 /plugin:review"}
        system = {"type": "system", "subtype": "local_command", "content": '<slash-command name="review"/>'}
        self.assertEqual(self.read(wrapped, wrapped + "What happened?", "/review --short\nKeep this.", "/review",
                                   "`/review` is a command.", "/tmp/example is a path.", system), [
            ("user", "", False, [annotation]),
            ("user", "What happened?", False, [annotation]),
            ("user", "--short\nKeep this.", False, [{"kind": "command", "label": "执行了命令 /review"}]),
            ("user", "", False, [{"kind": "command", "label": "执行了命令 /review"}]),
            ("user", "`/review` is a command.", False),
            ("user", "/tmp/example is a path.", False),
            ("event", "", False, [{"kind": "command", "label": "执行了命令 /review"}]),
        ])

    def test_image_blocks_show_annotations_without_paths_or_payloads(self):
        image = {"type": "image", "path": "/tmp/attached-image.png",
                 "source": {"type": "base64", "media_type": "image/png", "data": "c3ludGhldGlj"}}
        self.assertEqual(self.read([image], [
            {"type": "text", "text": ARTIFACT_CONTEXT + "Compare this"}, image,
            {"type": "text", "text": "with this"}, image,
        ]), [
            ("user", "", False, [IMAGE]),
            ("user", "Compare this\n\nwith this", False, [VIEWING, IMAGE, IMAGE]),
        ])

    def test_text_image_references_hide_the_attachment_path(self):
        self.assertEqual(self.read("[Image: source: /tmp/example screenshot.png]\nPlease inspect it.",
                                   "Please inspect /tmp/example screenshot.png."), [
            ("user", "Please inspect it.", False, [IMAGE]),
            ("user", "Please inspect /tmp/example screenshot.png.", False),
        ])

#!/usr/bin/env python3
import json
import sys

import local_status_monitor as monitor


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        question = str(payload.get("question") or "").strip()
        symbol = str(payload.get("symbol") or "").strip()
        chat_id = str(payload.get("chat_id") or "web").strip() or "web"
        env_file = str(payload.get("env_file") or monitor.ENV_PATH)

        env = monitor.load_env(monitor.Path(env_file))
        env["AI_ANALYSIS_MAX_TOKENS"] = str(max(int(env.get("AI_ANALYSIS_MAX_TOKENS") or 0), 2400))
        env["AI_ANALYSIS_TIMEOUT_SEC"] = str(max(int(env.get("AI_ANALYSIS_TIMEOUT_SEC") or 0), 70))
        if symbol:
            answer = monitor.run_ai_symbol_analysis(env, symbol, chat_id=chat_id, user_question=question)
        else:
            answer = monitor.run_ai_global_analysis(env, question, chat_id=chat_id)

        print(json.dumps({"success": True, "answer": answer}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)[:800]}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

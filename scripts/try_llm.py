from core.llm import TaskType, generate


def main() -> None:
    r = generate("Reply with exactly one word: pong", TaskType.GENERAL)
    print(r.text)
    print(
        f"model={r.model} in={r.input_tokens} "
        f"out={r.output_tokens} latency={r.latency_ms}ms"
    )


if __name__ == "__main__":
    main()
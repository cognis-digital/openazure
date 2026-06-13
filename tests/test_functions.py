import pytest

from openazure.errors import NotFound


def test_http_function_invoke(functions):
    @functions.http_function("hello")
    def hello(req):
        name = (req.get("params") or {}).get("name", "world")
        return {"status": 200, "body": f"hello {name}"}

    res = functions.invoke_http("hello", {"params": {"name": "azure"}})
    assert res["status"] == 200
    assert res["body"] == "hello azure"


def test_http_function_defaults(functions):
    @functions.http_function("echo")
    def echo(req):
        return {"body": req["body"]}

    res = functions.invoke_http("echo", {"body": "ping"})
    assert res["status"] == 200  # defaulted
    assert res["body"] == "ping"


def test_http_function_non_dict_return(functions):
    functions.register_http("plain", lambda req: "just text")
    res = functions.invoke_http("plain")
    assert res["body"] == "just text"
    assert res["status"] == 200


def test_invoke_missing_function(functions):
    with pytest.raises(NotFound):
        functions.invoke_http("nope")


def test_list_http(functions):
    functions.register_http("a", lambda r: {})
    functions.register_http("b", lambda r: {})
    assert functions.list_http() == ["a", "b"]


def test_queue_trigger_processes_and_deletes(functions, queue):
    queue.create_queue("work")
    queue.enqueue("work", "job-1")
    queue.enqueue("work", "job-2")

    seen = []

    @functions.queue_function("worker", "work")
    def worker(msg):
        seen.append(msg["body"])

    processed = functions.poll_queue("worker")
    assert processed == 2
    assert seen == ["job-1", "job-2"]
    assert queue.count("work") == 0  # deleted on success


def test_queue_trigger_failure_keeps_message(functions, queue):
    queue.create_queue("work")
    queue.enqueue("work", "poison")

    @functions.queue_function("bad", "work")
    def bad(msg):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        functions.poll_queue("bad", visibility_timeout=0.2)
    # message not deleted; still present in the queue
    assert queue.count("work") == 1


def test_queue_trigger_no_messages(functions, queue):
    queue.create_queue("empty")
    functions.register_queue("w", "empty", lambda m: None)
    assert functions.poll_queue("w") == 0


def test_runner_requires_queue_service():
    from openazure.functions import FunctionRunner
    fr = FunctionRunner(queue_service=None)
    fr.register_queue("w", "q", lambda m: None)
    with pytest.raises(RuntimeError):
        fr.poll_queue("w")

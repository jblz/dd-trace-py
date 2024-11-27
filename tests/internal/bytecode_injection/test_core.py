import dis
from ddtrace.internal.bytecode_injection.core import inject_invocation, InjectionContext


def test_linetable_honored_when_no_injection():
    original = sample_function_1.__code__
    ic = InjectionContext(original, _sample_callback, lambda _: [])
    injected, _ = inject_invocation(ic, 'some/path.py', 'some.package')

    assert list(original.co_lines()) == list(injected.co_lines())
    assert dict(dis.findlinestarts(original)) == dict(dis.findlinestarts(injected))


def sample_function_1():
    a = 1
    b = 2
    _ = a + b


def _sample_callback(*arg):
    print('callback')

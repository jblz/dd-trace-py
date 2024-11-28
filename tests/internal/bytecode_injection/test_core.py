import dis
from ddtrace.internal.bytecode_injection.core import inject_invocation, InjectionContext


def test_linetable_unchanged_when_no_injection():
    original = sample_function_1.__code__
    ic = InjectionContext(original, _sample_callback, lambda _: [])
    injected, _ = inject_invocation(ic, 'some/path.py', 'some.package')

    assert list(original.co_lines()) == list(injected.co_lines())
    assert dict(dis.findlinestarts(original)) == dict(dis.findlinestarts(injected))


def test_injection_works():

    accumulate = []

    def will_be_injected():
        accumulate.append(1)
        # in this spot we are going to inject accumulate(2)
        accumulate.append(3)

    dis.dis(will_be_injected, show_caches=True)

    def accumulate_2(*args):
        print('in injected code')
        accumulate.append(2)

    original = will_be_injected.__code__
    # From dis.dis(will_be_injected), 46 is the opcode index of `accumulate.append(3)`
    ic = InjectionContext(original, accumulate_2, lambda _: [46])
    injected, _ = inject_invocation(ic, 'some/path.py', 'some.package')
    will_be_injected.__code__ = injected

    will_be_injected()

    assert accumulate == [1, 2, 3]


def sample_function_1():
    a = 1
    b = 2
    _ = a + b


def _sample_callback(*arg):
    print('callback')
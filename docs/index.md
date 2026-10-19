# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

This is the initial package scaffold. It validates static declarations with
`--telescope-plan` and explicitly refuses `--telescope`; it does not yet provide
sandboxing or cached results. Ordinary pytest remains unchanged when telescope mode is inactive.

Python 3.11 or newer and pytest 8.3 through 9.x are the initial scaffold's supported
versions. The intended enforcement backend will target Linux first. No backend is
implemented or qualified yet.

- [Product specification](specification.md): intended guarantees and acceptance cases.
- [Local development](development.md): install, build, and verify the scaffold.
- [Publishing](publishing.md): documentation hosting and package release setup.

The [implementation queue](https://github.com/xu-hao/pytest-telescope/issues/3)
records the remaining work in dependency order. No benchmark results exist yet.

"""Finite native lookup and process proofs over already frozen mappings only.

Ancestry is deliberately overapproximated. Every RPATH block in a process must
miss or agree when an edge uses fallback; we never guess a convenient load order.
"""

import posixpath
from pathlib import Path

from . import tool_native as native
from .tool_context import MAX_LINK_HOPS, MAX_RELATIONS, digest, unsupported
from .tool_graph import layout

MAX_STATES = 65536
POLICY = "native-ancestor-neutrality-v1"


class Limit:
    def __init__(self, budget):
        self.budget = budget
        self.states = 0

    def step(self):
        self.budget.tick(visit=True)

    def retain(self):
        self.step()
        if self.states >= MAX_STATES:
            unsupported("bounds", "native ordering proof exceeds its state limit")
        self.states += 1


class Lookup:
    def __init__(self, runtime, limit, originals=None):
        self.runtime = runtime
        self.graph = runtime.graph
        self.limit = limit
        self.originals = originals
        self.logical_prefix = "/tools/" + self.graph.tool.name + "/"
        self.names = {}
        for filename in self.graph.mapping:
            self._index(filename)
        if originals is not None:
            for filename in originals:
                self._index(filename)
        self.tokens = {}
        self.information = {}
        self.slots = {}
        self.answers = {}
        self.load_aliases = {}
        self.links = []
        for key, row in self.graph.nodes.items():
            self.limit.step()
            if row["kind"] == "symlink":
                self.limit.retain()
                self.links.append(key)

    def _index(self, filename):
        self.limit.step()
        self.names.setdefault(posixpath.basename(filename), set()).add(filename)

    def token(self, pair):
        if pair not in self.tokens:
            self.limit.retain()
            graph, key = pair
            self.tokens[pair] = digest(graph.token(key))
        return self.tokens[pair]

    def elf(self, pair):
        graph, key = pair
        if graph is self.graph:
            return self.runtime.elf(key)
        if pair not in self.information:
            self.limit.retain()
            with graph.open(key) as stream:
                self.information[pair] = native.read_elf(stream, graph.nodes[key]["size"])
        return self.information[pair]

    def resolve(self, filename):
        self.limit.step()
        if self.originals is None or filename.startswith(self.logical_prefix):
            key = self.graph.resolve(filename, required=False)
            return None if key is None else (self.graph, key)
        seen = set()
        for _ in range(MAX_LINK_HOPS):
            self.limit.step()
            filename = self.graph.normalize_prefix(posixpath.normpath(filename))
            if filename in seen:
                layout("selected tool union contains a link cycle")
            seen.add(filename)
            parts = Path(filename).parts
            for end in range(2, len(parts) + 1):
                self.limit.step()
                prefix = str(Path(*parts[:end]))
                pair = self.originals.get(prefix)
                if pair is not None and pair[0].nodes[pair[1]]["kind"] == "symlink":
                    filename = posixpath.join(posixpath.dirname(prefix), pair[0].nodes[pair[1]]["target"], *parts[end:])
                    break
            else:
                return self.originals.get(filename)
        layout("selected tool union link traversal exceeds its bound")

    def candidate(self, filename):
        self.limit.step()
        if not filename.startswith(self.logical_prefix) and not self.runtime.covered(posixpath.dirname(filename)):
            layout("native search candidate lies outside the declared closed directories")
        pair = self.resolve(filename)
        if pair is not None:
            row = pair[0].nodes[pair[1]]
            if row["kind"] != "file" or not row["mode"] & 0o444 or self.elf(pair) is None:
                layout("native search candidate is not a readable supported pinned ELF")
        return pair

    def slot(self, directory, name):
        """A directory winner is independent of host hwcaps, or it refuses."""
        self.limit.step()
        memo = (directory, name)
        if memo in self.slots:
            return self.slots[memo]
        direct = self.candidate(posixpath.join(directory, name))
        normalized = self.graph.resolved_path(directory)
        count = 0
        for filename in self.names.get(name, ()):
            self.limit.step()
            if not Path(filename).parent.is_relative_to(normalized):
                continue
            count += 1
            if count > MAX_RELATIONS:
                layout("native lookup exceeds its candidate bound")
            alternative = self.candidate(filename)
            if alternative is not None and (direct is None or self.token(alternative) != self.token(direct)):
                layout("native search directory has an unverifiable alternative")
        self.limit.retain()
        self.slots[memo] = direct
        return direct

    def own(self, info, name, alias):
        winner = None
        # Validate shadowed slots too: proof never grants an unchecked path.
        for directory in native.search_paths(info, alias):
            self.limit.step()
            current = self.slot(directory, name)
            if winner is None:
                winner = current
        return winner

    def find(self, info, name, alias):
        self.limit.step()
        memo = (alias, name)
        if memo in self.answers:
            return self.answers[memo]
        if "/" in name:
            if not name.startswith("/") or posixpath.normpath(name) != name:
                layout("relative native dependency names are unsupported")
            winner = self.candidate(name)
            own = True
        else:
            winner = self.own(info, name, alias)
            own = winner is not None
            fallback = {}
            candidates = set()
            for directory in native.DEFAULT_SEARCH:
                self.limit.step()
                candidates.add(posixpath.join(directory, name))
                normalized = self.graph.resolved_path(directory)
                for filename in self.names.get(name, ()):
                    self.limit.step()
                    if Path(filename).parent.is_relative_to(normalized):
                        if len(candidates) >= MAX_RELATIONS and filename not in candidates:
                            layout("native lookup exceeds its candidate bound")
                        candidates.add(filename)
            for filename, _ in self.runtime.cache.get(name, ()):
                self.limit.step()
                if len(candidates) >= MAX_RELATIONS and filename not in candidates:
                    layout("native lookup exceeds its candidate bound")
                candidates.add(filename)
            for filename in candidates:
                current = self.candidate(filename)
                if current is not None:
                    fallback[self.token(current)] = current
            if not own:
                if len(fallback) != 1:
                    layout("native dependency is absent or ambiguous in the frozen view")
                winner = next(iter(fallback.values()))
        if winner is None:
            layout("native dependency is absent in the frozen view")
        self.limit.retain()
        self.answers[memo] = (winner, own)
        return winner, own

    def aliases(self, key):
        """Include declared file/directory links and the frozen baseline prefixes."""
        token = self.token((self.graph, key))
        if token in self.load_aliases:
            return self.load_aliases[token]
        aliases = set()

        def add(filename):
            self.limit.step()
            if filename not in aliases:
                self.limit.retain()
                aliases.add(filename)

        original = self.graph.original(key)
        for candidate in self.runtime.elfs:
            self.limit.step()
            if self.token((self.graph, candidate)) == token:
                add(self.graph.original(candidate))
                add(self.graph.logical(candidate))
        for link in self.links:
            self.limit.step()
            target = self.graph.resolve(self.graph.logical(link))
            destination = self.graph.original(target)
            if destination == original:
                suffix = ""
            elif self.graph.nodes[target]["kind"] == "directory" and Path(original).is_relative_to(destination):
                suffix = str(Path(original).relative_to(destination))
            else:
                continue
            for spelling in (self.graph.original(link), self.graph.logical(link)):
                add(posixpath.normpath(posixpath.join(spelling, suffix)))
        for prefix, (row, _) in self.graph.prefixes.items():
            self.limit.step()
            if row["kind"] == "symlink" and Path(original).is_relative_to(row["mapping"]):
                add(posixpath.join(prefix, str(Path(original).relative_to(row["mapping"]))))
        self.limit.retain()
        self.load_aliases[token] = aliases
        return aliases


class Proof:
    def __init__(self, runtimes, limit, originals=None, worker=None):
        self.limit = limit
        self.lookups = {runtime: Lookup(runtime, limit, originals) for runtime in runtimes}
        self.contexts = {}
        self.results = []
        self.worker = worker
        self.required_worker = {}

    def token(self, runtime, key):
        return self.lookups[runtime].token((runtime.graph, key))

    def add_context(self, runtime, root, seeds):
        context = self.token(runtime, root)
        if context not in self.contexts:
            self.limit.retain()
            self.contexts[context] = set()
        pending = self.contexts[context]
        for key in seeds:
            self.limit.step()
            pair = (runtime, key)
            if pair not in pending:
                self.limit.retain()
                pending.add(pair)

    def run(self):
        # Reprove every selected object, including unattached libraries. The
        # latter are not a promise about unspecified dynamic-load processes.
        for runtime, lookup in self.lookups.items():
            for key, info in runtime.elfs.items():
                self.limit.step()
                for name in info.needed:
                    self.limit.step()
                    expected = runtime.needed_targets[key, name]
                    for alias in (runtime.graph.original(key), runtime.graph.logical(key)):
                        pair, _ = lookup.find(info, name, alias)
                        if lookup.token(pair) != expected:
                            layout("selected tool union changes a native dependency winner")
            for root, seeds in runtime.processes(self.limit.budget):
                self.add_context(runtime, root, seeds)
            if self.worker is not None:
                executable, directories = self.worker
                key = runtime.graph.resolve(executable, required=False)
                if key in runtime.executables:
                    token = self.token(runtime, key)
                    if token not in self.required_worker:
                        self.limit.retain()
                        self.required_worker[token] = set()
                    required = self.required_worker[token]
                    for candidate in runtime.elfs:
                        self.limit.step()
                        if any(Path(runtime.graph.original(candidate)).is_relative_to(directory) for directory in directories):
                            value = self.token(runtime, candidate)
                            if value not in required:
                                self.limit.retain()
                                required.add(value)
        for context, seeds in self.contexts.items():
            self.context(context, seeds)
        return digest([POLICY, sorted(self.results)])

    def context(self, context, seeds):
        members = set()
        queued = set(seeds)
        pending = list(seeds)
        while pending:
            self.limit.step()
            runtime, key = pending.pop()
            pair = (runtime, key)
            members.add(pair)
            for child in runtime.dependencies[key]:
                self.limit.step()
                value = (runtime, child)
                if value not in queued:
                    self.limit.retain()
                    queued.add(value)
                    pending.append(value)
            info = runtime.elfs[key]
            if info.interpreter is not None:
                child = runtime.graph.resolve(info.interpreter)
                value = (runtime, child)
                if value not in queued:
                    self.limit.retain()
                    queued.add(value)
                    pending.append(value)
        blocks = [pair for pair in members if pair[0].elfs[pair[1]].rpath]
        actual = {self.token(runtime, key) for runtime, key in members}
        if not self.required_worker.get(context, set()) <= actual:
            layout("native worker lacks explicit native stdlib resource associations")
        names = {}

        def bind(name, token):
            self.limit.step()
            previous = names.get(name)
            if previous is not None and previous != token:
                layout("native process context has conflicting loaded names")
            if previous is None:
                self.limit.retain()
                names[name] = token

        targets = []
        for runtime, key in members:
            self.limit.step()
            lookup = self.lookups[runtime]
            graph, info = runtime.graph, runtime.elfs[key]
            token = self.token(runtime, key)
            for filename in lookup.aliases(key):
                bind(filename, token)
            if info.soname is not None:
                bind(info.soname, token)
            for name in info.needed:
                self.limit.step()
                expected = runtime.needed_targets[key, name]
                bind(name, expected)
                self.limit.retain()
                targets.append((token, name, expected))
                for alias in (graph.original(key), graph.logical(key)):
                    _, own = lookup.find(info, name, alias)
                    if own or info.runpath:
                        continue
                    for ancestor, ancestor_key in blocks:
                        other = self.lookups[ancestor]
                        for block_alias in (ancestor.graph.original(ancestor_key), ancestor.graph.logical(ancestor_key)):
                            self.limit.step()
                            selected = other.own(ancestor.elfs[ancestor_key], name, block_alias)
                            if selected is not None and other.token(selected) != expected:
                                layout("native process RPATH changes a fallback dependency")
        self.limit.retain()
        # The result exposes only a digest. Names and paths never enter reports.
        self.results.append((context, digest([sorted(names.items()), sorted(targets)])))


def validate_processes(runtimes, budget, originals=None, worker=None):
    selected = tuple(runtime for runtime in runtimes if runtime.native)
    return Proof(selected, Limit(budget), originals, worker).run()

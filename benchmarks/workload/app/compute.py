"""CPU-bound work in pure Python, with fixed inputs and known results."""

import hashlib


def chain(seed, rounds):
    value = seed.encode()
    for _ in range(rounds):
        value = hashlib.sha256(value).digest()
    return value.hex()


def longest_collatz(limit):
    best, best_start = 0, 1
    for start in range(1, limit):
        steps, value = 0, start
        while value != 1:
            value = value // 2 if value % 2 == 0 else 3 * value + 1
            steps += 1
        if steps > best:
            best, best_start = steps, start
    return best_start, best

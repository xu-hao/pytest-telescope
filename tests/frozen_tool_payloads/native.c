#include <stdio.h>

extern const char *owned_answer(void);

int main(int argc, char **argv) {
    if (argc != 2) return 23;
    printf("%s %s\n", owned_answer(), argv[1]);
    return 0;
}

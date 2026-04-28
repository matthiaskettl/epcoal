extern int __VERIFIER_nondet_int(void);

void reach_error() {  }

int main() {

    int i = __VERIFIER_nondet_int();
    int j = __VERIFIER_nondet_int();

    if (!(i >= 0 || j >= 0)) {
        return 0;
    }

    int x = i;
    int y = j;
    while (x != 0 && y != 0) {
        x--; 
        y--;
    }

    if (i == j && y != 0) {
        reach_error();
    }

    return 0;
}
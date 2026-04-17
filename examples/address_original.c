void change_int(int *i) {
    *i = 42;
}


int main() {
    int i;
    change_int(&i);
    if (i == 42) {
        return 1;
    }
    return 0;
}
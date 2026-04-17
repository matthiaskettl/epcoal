void change_int(int *i) {
    *i = 41;
    *i = *i + 1;
}


int main() {
    int i;
    change_int(&i);
    if (i == 42) {
        return 1;
    }
    return 0;
}
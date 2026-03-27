extern void abort(void);

int x;

int test() {
  x++;
  abort();
}

int main() {
   x++;
}
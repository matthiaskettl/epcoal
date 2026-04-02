extern void abort(void);

void reach_error() {  }

int x;

int test() {
  x++;
  reach_error();
}

int main() {
   x++;
   test();
}
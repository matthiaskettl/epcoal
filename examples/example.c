extern int __VERIFIER_nondet_int();

struct Point {
  int x;
  int y;
};

int x = 0;

void reach_error() {  }

int alpha(int x) {
  if (x) x++;
  while (x == 0) {int i = 0; i++; x = i;}
  while (x == 0) {int i = 0; i++; x = i;}
  if (x == 0) {reach_error();}
}

int main() {
  int x = __VERIFIER_nondet_int() + 10 + __VERIFIER_nondet_int();
  struct Point p = {0, 0};
  p.x = __VERIFIER_nondet_int();
  p.y = __VERIFIER_nondet_int();
  alpha(x);
  if (__VERIFIER_nondet_int() + 8) x++;
  while (x == 0) {int i = 0; i++; x = i;}
  while (x == 0) {int i = 0; i++; x = i;}
  if (x == 0) {reach_error();}
}

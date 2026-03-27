extern int __VERIFIER_nondet_int();

int x = 0;

void reach_error() {  }

int alpha(int x) {
  if (x) x--;
  while (x == 0) {int i = 0; i++; x = i;}
  while (x == 0) {int i = 0; i++; x = i;}
  if (x == 0) {reach_error();}
}

int main() {
  x++;
  int x = __VERIFIER_nondet_int();
  alpha(x);
  if (__VERIFIER_nondet_int()) x++;
  while (x == 0) {int i = 0; i++; x = i;}
  while (x == 0) {int i = 0; i++; x = i;}
  if (x == 0) {reach_error();}
}

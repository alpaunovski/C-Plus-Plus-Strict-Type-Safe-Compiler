// Positive experimental-native example. The dynamic bound determines the tracked
// allocation extent; normal array use prints 30 and matched delete[] completes.
#include <cstdio>

int main() {
  int count = 3;
  // The compiler tracks allocation size and initialization without changing pointer syntax.
  int* values = new int[count]{10, 20, 30};
  std::printf("%d\n", values[2]);
  // The array allocation family must be preserved through deallocation.
  delete[] values;
}

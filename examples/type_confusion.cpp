// Negative native example: a void-pointer round trip does not change the int object's
// stored type. The attempted float read is rejected before printf executes; expected
// runtime diagnostic is type-mismatch with exit 86.
#include <cstdio>

int main() {
  int value = 42;
  // The original object identity follows the pointer through this legal untyped carrier.
  void* opaque = &value;
  float* wrong = static_cast<float*>(opaque);
  // The cast itself remains legal. The following access is stopped.
  std::printf("%f\n", *wrong);
}

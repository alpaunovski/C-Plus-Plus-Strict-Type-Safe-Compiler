// Negative native example showing checks inside emitted standard-library templates.
// The source reference remains in scope while reserve invalidates its old buffer.
#include <vector>

int main() {
  std::vector<int> values{1};
  // The saved reference retains the allocation generation it originally referred to.
  int& reference = values[0];
  values.reserve(100); // Reallocation retires the old allocation's identity.
  // Expected runtime result: expired-object, exit 86. The vector itself remains valid,
  // but its old element storage does not.
  return reference;    // Native backend stops this stale access.
}

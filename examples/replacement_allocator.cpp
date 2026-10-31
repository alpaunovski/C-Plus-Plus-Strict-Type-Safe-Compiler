// Positive compatibility example for replaceable C++ allocation functions. The checker
// must allocate its own metadata without recursively calling this user operator new.
// Allocator returned-storage contracts still count as coverage gaps, so strict rejects this.
#include <cstdlib>
#include <new>

// Honor the ordinary scalar allocation contract, including non-null usable storage for
// a zero-size request and bad_alloc on failure.
void* operator new(std::size_t size) {
  if (!size) size = 1;
  if (void* pointer = std::malloc(size)) return pointer;
  throw std::bad_alloc();
}

// The runtime keeps the exact root identity live through this callback so delegating
// to C free does not incorrectly appear to be a malloc/delete-family mismatch.
void operator delete(void* pointer) noexcept {
  std::free(pointer);
}

// Read before deletion, then return a normal success status. No stale pointer is used.
int main() {
  int* pointer = new int(7);
  const int result = *pointer;
  delete pointer;
  return result != 7;
}

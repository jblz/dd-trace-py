#pragma once

#include "sample.hpp"

#include "vendored/concurrentqueue.h"

#include <memory>
#include <optional>

namespace Datadog {

class SynchronizedSamplePool
{
  private:
    moodycamel::ConcurrentQueue<Sample*> pool;
    size_t capacity;

  public:
    SynchronizedSamplePool(size_t _capacity): capacity(_capacity)
    {
        pool = moodycamel::ConcurrentQueue<Sample*>(_capacity);
    }

    std::optional<Sample*> take_sample();
    std::optional<Sample*> return_sample(Sample* sample);
};
} // namespace Datadog

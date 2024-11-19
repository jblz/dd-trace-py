#include "synchronized_sample_pool.hpp"

#include "libdatadog_helpers.hpp"

#include "vendored/concurrentqueue.h"

namespace Datadog {

std::optional<Sample*>
SynchronizedSamplePool::take_sample()
{
    Sample* sample = nullptr;

    pool.try_dequeue(sample);

    if (sample == nullptr) {
        return std::nullopt;
    }

    return sample;
}

std::optional<Sample*>
SynchronizedSamplePool::return_sample(Sample* sample)
{
    // We don't want the pool to grow without a bound, so check the size and
    // discard the sample if it's larger than capacity.
    if (pool.size_approx() >= capacity) {
        return sample;
    } else {
        pool.enqueue(sample);
        return std::nullopt;
    }
}
} // namespace Datadog

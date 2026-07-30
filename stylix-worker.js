/*
 * Browser port of Stylix's palette-generator.
 *
 * The color conversions, fitness function, crossover, mutation probability,
 * survivor count, and population size follow the Haskell implementation in
 * nix-community/stylix at revision
 * 66714e5ce44269ecc58c20d9196da8dbe1b27a31.
 *
 * The original uses Haskell's StdGen and vector quickselect. This port uses a
 * small seeded JavaScript PRNG and a streaming top-k heap, so it implements the
 * same search rather than promising byte-identical output.
 */

const SURVIVORS = 500;
const POPULATION = 50_000;
const MUTATION_PROBABILITY = 0.75;
const PALETTE_SIZE = 16;
const CHANNELS = 3;
const GENES = PALETTE_SIZE * CHANNELS;
const MAX_GENERATIONS = 120;

const targets = {
  dark: [10, 30, 45, 65, 75, 90, 95, 95, 60],
  light: [90, 70, 55, 35, 25, 10, 5, 5, 40]
};

const makeRandom = (seed) => {
  let state = seed >>> 0;
  return () => {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
};

const rgbToLab = (r, g, b, output, offset) => {
  let red = r / 255;
  let green = g / 255;
  let blue = b / 255;

  red = red > 0.04045 ? ((red + 0.055) / 1.055) ** 2.4 : red / 12.92;
  green = green > 0.04045 ? ((green + 0.055) / 1.055) ** 2.4 : green / 12.92;
  blue = blue > 0.04045 ? ((blue + 0.055) / 1.055) ** 2.4 : blue / 12.92;

  let x = (red * 0.4124 + green * 0.3576 + blue * 0.1805) / 0.95047;
  let y = red * 0.2126 + green * 0.7152 + blue * 0.0722;
  let z = (red * 0.0193 + green * 0.1192 + blue * 0.9505) / 1.08883;

  x = x > 0.008856 ? x ** (1 / 3) : 7.787 * x + 16 / 116;
  y = y > 0.008856 ? y ** (1 / 3) : 7.787 * y + 16 / 116;
  z = z > 0.008856 ? z ** (1 / 3) : 7.787 * z + 16 / 116;

  output[offset] = 116 * y - 16;
  output[offset + 1] = 500 * (x - y);
  output[offset + 2] = 200 * (y - z);
};

const labToRgb = (l, a, channelB) => {
  let y = (l + 16) / 116;
  let x = a / 500 + y;
  let z = y - channelB / 200;

  x = 0.95047 * (x ** 3 > 0.008856 ? x ** 3 : (x - 16 / 116) / 7.787);
  y = y ** 3 > 0.008856 ? y ** 3 : (y - 16 / 116) / 7.787;
  z = 1.08883 * (z ** 3 > 0.008856 ? z ** 3 : (z - 16 / 116) / 7.787);

  let red = x * 3.2406 + y * -1.5372 + z * -0.4986;
  let green = x * -0.9689 + y * 1.8758 + z * 0.0415;
  let blue = x * 0.0557 + y * -0.204 + z * 1.057;

  red = red > 0.0031308 ? 1.055 * red ** (1 / 2.4) - 0.055 : 12.92 * red;
  green = green > 0.0031308 ? 1.055 * green ** (1 / 2.4) - 0.055 : 12.92 * green;
  blue = blue > 0.0031308 ? 1.055 * blue ** (1 / 2.4) - 0.055 : 12.92 * blue;

  return [
    Math.trunc(Math.max(0, Math.min(1, red)) * 255),
    Math.trunc(Math.max(0, Math.min(1, green)) * 255),
    Math.trunc(Math.max(0, Math.min(1, blue)) * 255)
  ];
};

const deltaE = (palette, first, second) => {
  const deltaL = palette[first] - palette[second];
  const deltaA = palette[first + 1] - palette[second + 1];
  const deltaB = palette[first + 2] - palette[second + 2];
  const c1 = Math.hypot(palette[first + 1], palette[first + 2]);
  const c2 = Math.hypot(palette[second + 1], palette[second + 2]);
  const deltaC = c1 - c2;
  const squaredH = deltaA ** 2 + deltaB ** 2 - deltaC ** 2;
  const deltaH = squaredH < 0 ? 0 : Math.sqrt(squaredH);
  return Math.sqrt(
    deltaL ** 2 +
    (deltaC / (1 + 0.045 * c1)) ** 2 +
    (deltaH / (1 + 0.015 * c1)) ** 2
  );
};

const fitness = (polarity, palette) => {
  let primarySimilarity = 0;
  for (let first = 0; first < 8; first += 1) {
    for (let second = first + 1; second < 8; second += 1) {
      primarySimilarity = Math.max(
        primarySimilarity,
        deltaE(palette, first * CHANNELS, second * CHANNELS)
      );
    }
  }

  let accentDifference = Infinity;
  for (let first = 8; first < 16; first += 1) {
    for (let second = first + 1; second < 16; second += 1) {
      accentDifference = Math.min(
        accentDifference,
        deltaE(palette, first * CHANNELS, second * CHANNELS)
      );
    }
  }

  const target = targets[polarity];
  let schemeError = 0;
  for (let index = 0; index < 8; index += 1) {
    schemeError += Math.abs(target[index] - palette[index * CHANNELS]);
  }
  for (let index = 8; index < 16; index += 1) {
    schemeError += Math.abs(target[8] - palette[index * CHANNELS]);
  }

  return accentDifference - primarySimilarity / 10 - schemeError;
};

const copyPalette = (palette) => new Float64Array(palette);

const swap = (heap, left, right) => {
  const temporary = heap[left];
  heap[left] = heap[right];
  heap[right] = temporary;
};

const siftUp = (heap, start) => {
  let index = start;
  while (index > 0) {
    const parent = Math.floor((index - 1) / 2);
    if (heap[parent].score <= heap[index].score) break;
    swap(heap, parent, index);
    index = parent;
  }
};

const siftDown = (heap, start) => {
  let index = start;
  while (true) {
    const left = index * 2 + 1;
    const right = left + 1;
    let smallest = index;
    if (left < heap.length && heap[left].score < heap[smallest].score) smallest = left;
    if (right < heap.length && heap[right].score < heap[smallest].score) smallest = right;
    if (smallest === index) return;
    swap(heap, index, smallest);
    index = smallest;
  }
};

const retain = (heap, palette, score) => {
  if (heap.length < SURVIVORS) {
    heap.push({ score, palette: copyPalette(palette) });
    siftUp(heap, heap.length - 1);
    return;
  }
  if (score <= heap[0].score) return;
  heap[0].score = score;
  heap[0].palette.set(palette);
  siftDown(heap, 0);
};

const paletteFromImage = (pixels, random) => {
  const palette = new Float64Array(GENES);
  for (let index = 0; index < PALETTE_SIZE; index += 1) {
    const pixel = Math.floor(random() * (pixels.length / 4)) * 4;
    rgbToLab(pixels[pixel], pixels[pixel + 1], pixels[pixel + 2], palette, index * 3);
  }
  return palette;
};

const mutate = (palette, pixels, random) => {
  if (random() > MUTATION_PROBABILITY) return;
  const gene = Math.floor(random() * PALETTE_SIZE) * CHANNELS;
  const pixel = Math.floor(random() * (pixels.length / 4)) * 4;
  rgbToLab(pixels[pixel], pixels[pixel + 1], pixels[pixel + 2], palette, gene);
};

const crossover = (left, right, output) => {
  for (let index = 0; index < PALETTE_SIZE; index += 1) {
    const parent = index % 2 === 0 ? left : right;
    const offset = index * CHANNELS;
    output[offset] = parent[offset];
    output[offset + 1] = parent[offset + 1];
    output[offset + 2] = parent[offset + 2];
  }
};

const toHex = (value) => value.toString(16).padStart(2, "0");

const outputPalette = (palette) => {
  const output = {};
  for (let index = 0; index < PALETTE_SIZE; index += 1) {
    const offset = index * CHANNELS;
    const [red, green, blue] = labToRgb(
      palette[offset],
      palette[offset + 1],
      palette[offset + 2]
    );
    output[`base${index.toString(16).padStart(2, "0").toUpperCase()}`] =
      `${toHex(red)}${toHex(green)}${toHex(blue)}`;
  }
  return output;
};

const evolve = (pixels, polarity, seed) => {
  const random = makeRandom(seed);
  let survivors = Array.from(
    { length: SURVIVORS },
    () => paletteFromImage(pixels, random)
  );
  let previousBest;

  for (let generation = 1; generation <= MAX_GENERATIONS; generation += 1) {
    const heap = [];

    retain(heap, survivors[0], fitness(polarity, survivors[0]));

    for (let index = 1; index < SURVIVORS; index += 1) {
      const candidate = copyPalette(survivors[index]);
      mutate(candidate, pixels, random);
      retain(heap, candidate, fitness(polarity, candidate));
    }

    const candidate = new Float64Array(GENES);
    for (let index = SURVIVORS; index < POPULATION; index += 1) {
      const left = survivors[Math.floor(random() * survivors.length)];
      const right = survivors[Math.floor(random() * survivors.length)];
      crossover(left, right, candidate);
      mutate(candidate, pixels, random);
      retain(heap, candidate, fitness(polarity, candidate));
    }

    heap.sort((left, right) => right.score - left.score);
    survivors = heap.map((entry) => entry.palette);
    const best = heap[0].score;
    postMessage({ type: "progress", generation, fitness: best });

    if (best === previousBest) {
      return { palette: outputPalette(survivors[0]), generations: generation, fitness: best };
    }
    previousBest = best;
  }

  return {
    palette: outputPalette(survivors[0]),
    generations: MAX_GENERATIONS,
    fitness: fitness(polarity, survivors[0])
  };
};

self.addEventListener("message", (event) => {
  if (event.data?.type !== "generate") return;
  const { pixels, polarity, seed, version } = event.data;
  try {
    if (!(pixels instanceof Uint8ClampedArray) || pixels.length < 4) {
      throw new Error("The image did not contain readable pixels.");
    }
    if (!(polarity in targets)) throw new Error("Invalid palette polarity.");
    const result = evolve(pixels, polarity, seed >>> 0);
    postMessage({ type: "complete", version, ...result });
  } catch (error) {
    postMessage({
      type: "error",
      version,
      message: error instanceof Error ? error.message : "Palette generation failed."
    });
  }
});

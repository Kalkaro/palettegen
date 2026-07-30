const NEKO_API = "https://nekos.best/api/v2/neko";
const MAX_SAMPLE_PIXELS = 1_500_000;
const MAX_HISTORY = 20;
const DATABASE_NAME = "stylix-palette-generator";
const DATABASE_VERSION = 1;
const STORE_NAME = "palettes";

const root = document.documentElement;
const title = document.querySelector("h1");
const stage = document.querySelector(".wallpaper");
let activeImage = stage.querySelector("img");
const loaderText = stage.querySelector(".loader span");
const meta = stage.querySelector(".meta");
const tags = stage.querySelector(".tags");
const source = stage.querySelector(".source");
const copyImageUrl = stage.querySelector(".copy-image-url");
const copyHash = stage.querySelector(".copy-hash");
const paletteDock = document.querySelector(".palette-dock");
const paletteElement = document.querySelector(".palette");
const paletteStatus = document.querySelector(".palette-status span:last-child");
const historyList = document.querySelector(".history-list");
const historyCount = document.querySelector(".history-count");
const generateButton = document.querySelector(".generate");
const uploadButton = document.querySelector(".upload-generate");
const fileInput = document.querySelector(".image-file");
const polarityToggle = document.querySelector(".polarity-toggle");
const urlGenerate = document.querySelector(".url-generate");
const toast = document.querySelector(".toast");

let toastTimer;
let generationVersion = 0;
let worker;
let records = [];
let currentRecord;
let databasePromise;
const objectUrls = new Set();

const playTitleSequence = () => {
  title.classList.remove("title-sequence");
  void title.offsetWidth;
  title.classList.add("title-sequence");
};

const readableInk = (hex) => {
  const [red, green, blue] = hex.match(/\w\w/g).map((part) => parseInt(part, 16) / 255);
  const linear = [red, green, blue].map((value) =>
    value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  );
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2] > 0.38
    ? "#18191c"
    : "#fafafa";
};

const notify = (message) => {
  toast.textContent = String(message).toLowerCase();
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2400);
};

const renderPalette = (colors) => {
  paletteElement.replaceChildren();
  Object.entries(colors).forEach(([name, raw], index) => {
    const hex = `#${raw.toLowerCase()}`;
    root.style.setProperty(`--${name}`, hex);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "swatch";
    button.style.cssText = `--i:${index};--color:${hex};--ink:${readableInk(raw)}`;
    const colorName = document.createElement("strong");
    const colorValue = document.createElement("small");
    colorName.textContent = name.toLowerCase();
    colorValue.textContent = hex;
    button.append(colorName, colorValue);
    button.addEventListener("click", async () => {
      await navigator.clipboard.writeText(hex);
      notify(`copied ${hex}`);
    });
    paletteElement.append(button);
  });
};

const setStageLoading = (active, message = "") => {
  stage.classList.toggle("loading", active);
  if (message) loaderText.textContent = message;
};

const setBusy = (active, label = "generating…") => {
  generateButton.textContent = active ? "skip?" : "generate again";
  generateButton.disabled = false;
  uploadButton.disabled = active;
  urlGenerate.disabled = active;
  if (active) paletteStatus.textContent = label;
};

const setPaletteGenerating = (active) => {
  paletteDock.classList.toggle("generating", active);
};

const recordImageUrl = (record) => {
  if (!record.objectUrl) {
    record.objectUrl = URL.createObjectURL(record.blob);
    objectUrls.add(record.objectUrl);
  }
  return record.objectUrl;
};

const displayWallpaper = async (record) => {
  meta.classList.remove("visible");
  const incomingImage = document.createElement("img");
  incomingImage.alt = record.tags || "palette source image";
  incomingImage.decoding = "async";
  stage.insertBefore(incomingImage, stage.querySelector(".shade"));

  await new Promise((resolve, reject) => {
    incomingImage.onload = resolve;
    incomingImage.onerror = () => {
      incomingImage.remove();
      reject(new Error("the image could not be displayed"));
    };
    incomingImage.src = recordImageUrl(record);
  });

  tags.textContent = record.tags || "your image";
  source.href = record.post_url || record.image_url || "#";
  source.textContent = record.source_label || "open image source";
  source.hidden = !record.post_url && !record.image_url;
  copyImageUrl.dataset.value = record.image_url || "";
  copyImageUrl.hidden = !record.image_url;
  copyHash.dataset.value = record.sha256 || "";
  copyHash.hidden = !record.sha256;
  stage.classList.add("has-image");

  const previousImage = activeImage;
  activeImage = incomingImage;
  void incomingImage.offsetWidth;
  requestAnimationFrame(() => {
    incomingImage.classList.add("loaded");
    meta.classList.add("visible");
  });
  setTimeout(() => {
    if (previousImage !== activeImage) previousImage.remove();
  }, 1450);
};

const displayRecord = async (record) => {
  currentRecord = record;
  await displayWallpaper(record);
  renderPalette(record.palette);
  setPaletteGenerating(false);
};

const sizeHistoryToFive = () => {
  if (innerWidth <= 780) {
    historyList.style.maxHeight = "";
    return;
  }
  const firstCard = historyList.querySelector(".history-card");
  if (!firstCard) {
    historyList.style.maxHeight = "";
    return;
  }
  const styles = getComputedStyle(historyList);
  const gap = parseFloat(styles.rowGap) || 0;
  const padding =
    (parseFloat(styles.paddingTop) || 0) + (parseFloat(styles.paddingBottom) || 0);
  historyList.style.maxHeight = `${firstCard.offsetHeight * 5 + gap * 4 + padding}px`;
};

const renderHistory = () => {
  historyList.replaceChildren();
  historyCount.textContent = `${records.length} generation${records.length === 1 ? "" : "s"}`;

  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "history-empty";
    empty.textContent = "generated wallpapers will stay in this browser.";
    historyList.append(empty);
    return;
  }

  records.forEach((record, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "history-card";
    card.style.setProperty("--i", Math.min(index, 12));
    card.title = record.tags;
    const thumbnail = document.createElement("img");
    thumbnail.src = recordImageUrl(record);
    thumbnail.alt = "";
    const timestamp = document.createElement("span");
    timestamp.textContent = new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short"
    }).format(new Date(record.created_at)).toLowerCase();
    card.append(thumbnail, timestamp);
    card.addEventListener("click", async () => {
      try {
        await displayRecord(record);
        window.scrollTo({ top: 0, behavior: "smooth" });
      } catch (error) {
        notify(error.message);
      }
    });
    historyList.append(card);
  });
  requestAnimationFrame(sizeHistoryToFive);
};

const openDatabase = () => {
  if (!databasePromise) {
    databasePromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore(STORE_NAME, { keyPath: "id" });
        store.createIndex("created_at", "created_at");
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }
  return databasePromise;
};

const transactionRequest = async (mode, callback) => {
  const database = await openDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, mode);
    const store = transaction.objectStore(STORE_NAME);
    const result = callback(store);
    transaction.oncomplete = () => resolve(result);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
};

const loadHistory = async () => {
  try {
    const database = await openDatabase();
    records = await new Promise((resolve, reject) => {
      const request = database
        .transaction(STORE_NAME)
        .objectStore(STORE_NAME)
        .getAll();
      request.onsuccess = () =>
        resolve(request.result.sort((left, right) => right.created_at.localeCompare(left.created_at)));
      request.onerror = () => reject(request.error);
    });
    renderHistory();
  } catch {
    historyCount.textContent = "unavailable";
  }
};

const saveRecord = async (record) => {
  const stored = { ...record };
  delete stored.objectUrl;
  await transactionRequest("readwrite", (store) => store.put(stored));
  records = [record, ...records.filter((item) => item.id !== record.id)];
  while (records.length > MAX_HISTORY) {
    const removed = records.pop();
    await transactionRequest("readwrite", (store) => store.delete(removed.id));
    if (removed.objectUrl) {
      URL.revokeObjectURL(removed.objectUrl);
      objectUrls.delete(removed.objectUrl);
    }
  }
  renderHistory();
};

const sha256 = async (blob) => {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", await blob.arrayBuffer()));
  let binary = "";
  for (const byte of digest) binary += String.fromCharCode(byte);
  return `sha256-${btoa(binary)}`;
};

const imagePixels = async (blob) => {
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, Math.sqrt(MAX_SAMPLE_PIXELS / (bitmap.width * bitmap.height)));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d", { alpha: false, willReadFrequently: true });
  context.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();
  return context.getImageData(0, 0, width, height).data;
};

const runWorker = (pixels, polarity, version) =>
  new Promise((resolve, reject) => {
    worker?.terminate();
    worker = new Worker("./stylix-worker.js");
    worker.addEventListener("message", (event) => {
      if (event.data.version !== undefined && event.data.version !== version) return;
      if (event.data.type === "progress") {
        paletteStatus.textContent = `stylix generation ${event.data.generation}…`;
      } else if (event.data.type === "complete") {
        worker.terminate();
        worker = undefined;
        resolve(event.data);
      } else if (event.data.type === "error") {
        worker.terminate();
        worker = undefined;
        reject(new Error(event.data.message));
      }
    });
    worker.addEventListener("error", (event) => {
      worker?.terminate();
      worker = undefined;
      reject(new Error(event.message || "palette worker failed"));
    });
    worker.postMessage(
      {
        type: "generate",
        pixels,
        polarity,
        seed: 0,
        version
      },
      [pixels.buffer]
    );
  });

const validateImageBlob = async (blob) => {
  if (!blob.type.startsWith("image/")) throw new Error("the selected file is not an image");
  if (blob.size > 25 * 1024 * 1024) throw new Error("the image is larger than 25 MB");
  const bitmap = await createImageBitmap(blob);
  if (bitmap.width < 32 || bitmap.height < 32) {
    bitmap.close();
    throw new Error("the image is too small");
  }
  bitmap.close();
};

const makeRecord = async (blob, details) => {
  const createdAt = new Date().toISOString();
  return {
    id: `${Date.now()}-${crypto.randomUUID()}`,
    blob,
    created_at: createdAt,
    sha256: await sha256(blob),
    polarity: polarityToggle.checked ? "light" : "dark",
    ...details
  };
};

const generateFromBlob = async (blob, details) => {
  const version = ++generationVersion;
  setBusy(true, "reading image…");
  setStageLoading(true, "reading image in your browser…");
  setPaletteGenerating(true);

  try {
    await validateImageBlob(blob);
    const record = await makeRecord(blob, details);
    await displayWallpaper(record);
    if (version !== generationVersion) return;
    currentRecord = record;
    setStageLoading(false);
    paletteStatus.textContent = "sampling image…";
    const pixels = await imagePixels(blob);
    if (version !== generationVersion) return;
    const result = await runWorker(pixels, record.polarity, version);
    if (version !== generationVersion) return;
    record.palette = result.palette;
    record.generations = result.generations;
    renderPalette(record.palette);
    setPaletteGenerating(false);
    await saveRecord(record);
    notify(`palette ready in ${result.generations} generations`);
  } catch (error) {
    if (version !== generationVersion) return;
    loaderText.textContent = error.message.toLowerCase();
    setPaletteGenerating(false);
    notify(error.message);
  } finally {
    if (version === generationVersion) {
      setStageLoading(false);
      setBusy(false);
    }
  }
};

const fetchImage = async (url) => {
  const response = await fetch(url, { mode: "cors" });
  if (!response.ok) throw new Error(`image server returned ${response.status}`);
  return response.blob();
};

const generateRandom = async () => {
  const version = ++generationVersion;
  worker?.terminate();
  worker = undefined;
  setBusy(true, "fetching a safe anime image…");
  setStageLoading(true, "fetching a safe anime image…");
  setPaletteGenerating(true);

  try {
    const response = await fetch(NEKO_API);
    if (!response.ok) throw new Error(`wallpaper provider returned ${response.status}`);
    const payload = await response.json();
    const image = payload?.results?.[0];
    if (!image?.url) throw new Error("wallpaper provider returned an invalid response");
    const blob = await fetchImage(image.url);
    if (version !== generationVersion) return;
    generationVersion -= 1;
    await generateFromBlob(blob, {
      tags: image.artist_name ? `art by ${image.artist_name}` : "safe anime artwork",
      post_url: image.source_url || image.artist_href || image.url,
      source_label: image.source_url ? "open original source" : "open image source",
      image_url: image.url
    });
  } catch (error) {
    if (version !== generationVersion) return;
    loaderText.textContent = error.message.toLowerCase();
    setPaletteGenerating(false);
    setStageLoading(false);
    setBusy(false);
    notify(error.message);
  }
};

const generateFromFile = (file) =>
  generateFromBlob(file, {
    tags: file.name,
    source_label: "local image"
  });

generateButton.addEventListener("click", () => {
  if (worker || stage.classList.contains("loading")) {
    generationVersion += 1;
    worker?.terminate();
    worker = undefined;
  }
  generateRandom();
});

uploadButton.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  const [file] = fileInput.files;
  fileInput.value = "";
  if (file) generateFromFile(file);
});

urlGenerate.addEventListener("click", async () => {
  const value = prompt("paste a CORS-enabled https image url");
  if (value === null) return;
  try {
    const imageUrl = new URL(value.trim());
    if (imageUrl.protocol !== "https:") throw new Error("please enter a valid https image url");
    setStageLoading(true, "fetching your image…");
    const blob = await fetchImage(imageUrl.href);
    await generateFromBlob(blob, {
      tags: "image from url",
      post_url: imageUrl.href,
      source_label: "open image source",
      image_url: imageUrl.href
    });
  } catch (error) {
    setStageLoading(false);
    notify(
      error instanceof TypeError
        ? "that image host does not allow browser access; upload the file instead"
        : error.message
    );
  }
});

stage.addEventListener("dragover", (event) => {
  if (!event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  stage.classList.add("dragging");
});
stage.addEventListener("dragleave", () => stage.classList.remove("dragging"));
stage.addEventListener("drop", (event) => {
  event.preventDefault();
  stage.classList.remove("dragging");
  const [file] = event.dataTransfer.files;
  if (file) generateFromFile(file);
});

title.addEventListener("click", playTitleSequence);
title.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    playTitleSequence();
  }
});
copyImageUrl.addEventListener("click", async () => {
  await navigator.clipboard.writeText(copyImageUrl.dataset.value);
  notify("copied image url");
});
copyHash.addEventListener("click", async () => {
  await navigator.clipboard.writeText(copyHash.dataset.value);
  notify("copied hash");
});
polarityToggle.addEventListener("change", () => {
  if (currentRecord?.blob) {
    generateFromBlob(currentRecord.blob, {
      tags: currentRecord.tags,
      post_url: currentRecord.post_url,
      source_label: currentRecord.source_label,
      image_url: currentRecord.image_url
    });
  }
});
addEventListener("resize", sizeHistoryToFive);
addEventListener("beforeunload", () => {
  worker?.terminate();
  for (const url of objectUrls) URL.revokeObjectURL(url);
});

loadHistory();

import { Component } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Stack from "@mui/material/Stack";

/** Ловит ошибки рендера, чтобы одна неудачная панель не превращала всё приложение в белый экран. */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error("UI error:", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Stack spacing={1.5} sx={{ p: 2 }}>
        <Alert severity="error">
          Произошла ошибка интерфейса: {String(this.state.error?.message || this.state.error)}
        </Alert>
        <Stack direction="row" spacing={1}>
          <Button variant="outlined" onClick={() => this.setState({ error: null })}>
            Повторить
          </Button>
          <Button variant="text" onClick={() => window.location.reload()}>
            Перезагрузить страницу
          </Button>
        </Stack>
      </Stack>
    );
  }
}
